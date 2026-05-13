from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from django.db import transaction
from django.db.models import F

from apps.public_core.models import ExtractedDocument, WellRegistry, ResearchSession
from apps.public_core.services.openai_extraction import (
    classify_document,
    extract_json_from_pdf,
    vectorize_extracted_document,
    SUPPORTED_TYPES,
)
from apps.kernel.services.jurisdiction_registry import detect_jurisdiction
from apps.public_core.services.adapters.base import DocumentSpec, StateAdapter
from apps.public_core.services.adapters.nm_adapter import NMAdapter
from apps.public_core.services.adapters.tx_adapter import TXAdapter

logger = logging.getLogger(__name__)

# Registry of state adapters
ADAPTER_REGISTRY: Dict[str, type] = {
    "NM": NMAdapter,
    "TX": TXAdapter,
}

# File extensions that cannot be processed by the PDF extraction pipeline.
# Documents with these extensions will be recorded with status='unsupported'
# so operators can distinguish format rejections from extraction failures.
# Valid status values: success | partial | error | unsupported
UNSUPPORTED_EXTENSIONS: set = {
    ".docx", ".xlsx", ".pptx",
    ".doc", ".xls", ".ppt",
    ".csv", ".txt",
}

# Jurisdiction-appropriate document type candidates for LLM classification fallback
TX_TYPES = ["w1", "w2", "w3", "w3a", "w15", "g1", "pa_procedure", "gau", "schematic", "formation_tops", "w12", "l1", "p14", "swr10", "swr13"]
NM_TYPES = ["c_100", "c_101", "c_102", "c_103", "c_104", "c_105", "sundry", "apd", "schematic", "formation_tops"]


def get_adapter(state: str) -> StateAdapter:
    """Get the appropriate adapter for the state."""
    cls = ADAPTER_REGISTRY.get(state.upper())
    if not cls:
        raise ValueError(f"No adapter registered for state: {state}")
    return cls()


def fetch_document_list(api_number: str, state: str) -> List[DocumentSpec]:
    """Fetch list of available documents using the state adapter."""
    adapter = get_adapter(state)
    return adapter.fetch_document_list(api_number)


def index_single_document(
    doc: DocumentSpec,
    api_number: str,
    well: Optional[WellRegistry],
    session: Optional[ResearchSession] = None,
) -> Optional[ExtractedDocument]:
    """
    Process a single document through the pipeline:
    download (if needed) -> classify -> extract -> create ExtractedDocument -> vectorize

    Returns the ExtractedDocument if successful, None if skipped/failed.
    Idempotency: checks for existing ExtractedDocument before re-extracting.
    """
    state = session.state if session else detect_jurisdiction(api_number)
    adapter = get_adapter(state)

    # Idempotency: check if already extracted (Neubus-aware)
    if doc.metadata and doc.metadata.get("neubus_lease_id"):
        # Neubus documents: check by neubus_filename (more precise)
        existing = ExtractedDocument.objects.filter(
            api_number=api_number,
            neubus_filename=doc.filename,
        ).first()
    else:
        # Legacy path: check by source_path
        existing = ExtractedDocument.objects.filter(
            api_number=api_number,
            source_path__icontains=doc.filename,
        ).first()
    if existing:
        # Only skip re-processing if the existing document was successfully extracted
        has_data = existing.json_data and existing.json_data != {}
        is_known_type = existing.document_type and existing.document_type != "unknown"
        if has_data and is_known_type:
            logger.info(f"Document already extracted: {doc.filename} -> ED {existing.id}")
            # Do NOT increment indexed_documents here — the caller (index_document_task)
            # will call _increment_and_maybe_finalize() after this function returns,
            # so counting here would double-count skipped documents.
            return existing
        else:
            logger.info(
                f"Re-processing previously failed document: {doc.filename} "
                f"(type={existing.document_type}, status={existing.status})"
            )
            existing.delete()

    # Download
    local_path = adapter.download_document(doc)

    # Unsupported extension guard — short-circuit before any OpenAI calls
    # Use .suffix directly (local_path may already be a Path object or a Path-like mock)
    suffix = local_path.suffix.lower() if hasattr(local_path, "suffix") else Path(local_path).suffix.lower()
    if suffix in UNSUPPORTED_EXTENSIONS:
        logger.info(f"Unsupported file extension '{suffix}' for {doc.filename} — skipping classification")
        ed = ExtractedDocument.objects.create(
            well=well,
            api_number=api_number,
            document_type="unknown",
            source_path=doc.url or (str(local_path) if local_path else doc.filename),
            neubus_filename=doc.filename if (doc.metadata and doc.metadata.get("neubus_lease_id")) else "",
            status="unsupported",
            json_data={},
            errors=[{"type": "unsupported_format", "message": f"File extension {suffix} is not supported"}],
        )
        if session:
            ResearchSession.objects.filter(id=session.id).update(
                failed_documents=F("failed_documents") + 1
            )
        return ed

    # Classify
    # Use pre-set doc_type if available and valid (NM adapter sets this from filename)
    if doc.doc_type and doc.doc_type in SUPPORTED_TYPES:
        doc_type = doc.doc_type
        logger.info(f"Using pre-set doc_type={doc_type} for {doc.filename}")
    else:
        # Build jurisdiction-appropriate candidate types for LLM fallback
        candidate_types = TX_TYPES if state == "TX" else NM_TYPES if state == "NM" else None
        doc_type = classify_document(local_path, candidate_types=candidate_types)
    if doc_type == "unknown":
        logger.warning(f"Could not classify document: {doc.filename}")
        # Persist the failure so it can be retried and provides an audit trail
        ed = ExtractedDocument.objects.create(
            well=well,
            api_number=api_number,
            document_type="unknown",
            source_path=doc.url or (str(local_path) if local_path else doc.filename),
            neubus_filename=doc.filename if (doc.metadata and doc.metadata.get("neubus_lease_id")) else "",
            status="error",
            json_data={},
            errors=[f"Could not classify document: {doc.filename}"],
        )
        if session:
            ResearchSession.objects.filter(id=session.id).update(
                failed_documents=F("failed_documents") + 1
            )
        return ed

    # Extract (with optional tag-aware prompts)
    try:
        from apps.public_core.services.segment_tagger import tag_segment
        _tags = tag_segment(doc_type)
    except Exception:
        _tags = None

    try:
        result = extract_json_from_pdf(local_path, doc_type, tags=_tags)
    except (KeyError, ValueError) as exc:
        # No extraction prompt for this doc type — store as classified but not extracted
        logger.info(f"No extraction prompt for {doc_type}, storing as classified: {exc}")
        from dataclasses import dataclass

        @dataclass
        class _Stub:
            json_data: dict
            raw_text: str
            errors: list
            model_tag: str

        # Try to get raw text for search
        raw_text = ""
        try:
            import pdfplumber
            with pdfplumber.open(str(local_path)) as pdf:
                raw_text = "\n".join((p.extract_text() or "") for p in pdf.pages[:5])[:10000]
        except Exception:
            pass
        if not raw_text.strip():
            try:
                from pdf2image import convert_from_path
                import pytesseract
                images = convert_from_path(str(local_path), first_page=1, last_page=3, dpi=150)
                raw_text = "\n".join(pytesseract.image_to_string(img) for img in images)[:10000]
            except Exception:
                pass

        result = _Stub(
            json_data={"_raw_text": raw_text} if raw_text else {},
            raw_text=raw_text,
            errors=[],
            model_tag="none",
        )

    # Store raw PDF text for fallback vector retrieval
    if result.raw_text:
        result.json_data["_raw_text"] = result.raw_text

    # Persist
    with transaction.atomic():
        ed = ExtractedDocument.objects.create(
            well=well,
            api_number=api_number,
            document_type=doc_type,
            source_path=doc.url or str(local_path),
            neubus_filename=doc.filename if (doc.metadata and doc.metadata.get("neubus_lease_id")) else "",
            model_tag=result.model_tag,
            status="success" if not result.errors else "partial",
            errors=result.errors,
            json_data=result.json_data,
        )

        # Create DocumentSegment for provenance tracking
        try:
            import fitz as _fitz
            from apps.public_core.models.document_segment import DocumentSegment
            _pdf = _fitz.open(str(local_path))
            _total_pages = len(_pdf)
            _pdf.close()

            source_type = "nm_ocd" if state == "NM" else "upload"
            if doc.metadata and doc.metadata.get("rrc_source"):
                source_type = "rrc"
            elif doc.metadata and doc.metadata.get("neubus_lease_id"):
                source_type = "neubus"

            DocumentSegment.objects.create(
                well=well,
                api_number=api_number,
                source_filename=doc.filename,
                source_path=str(local_path),
                file_hash="",
                source_type=source_type,
                page_start=0,
                page_end=max(0, _total_pages - 1),
                total_source_pages=_total_pages,
                form_type=doc_type,
                classification_method="filename" if doc.doc_type else "text",
                classification_confidence="high" if doc.doc_type else "medium",
                classification_evidence=f"Pre-set doc_type={doc.doc_type}" if doc.doc_type else "LLM classification",
                tags=[],
                status="extracted",
                extracted_document=ed,
                raw_text_cache="",
            )
        except Exception as seg_err:
            logger.warning(f"Failed to create DocumentSegment for {doc.filename}: {seg_err}")

        # Vectorize
        try:
            vectorize_extracted_document(ed)
        except Exception as e:
            logger.exception(f"Vectorization failed for {doc.filename}: {e}")

    # Update session progress
    if session:
        ResearchSession.objects.filter(id=session.id).update(
            indexed_documents=F("indexed_documents") + 1
        )

    logger.info(f"Indexed document: {doc.filename} -> ED {ed.id} (type={doc_type})")
    return ed

