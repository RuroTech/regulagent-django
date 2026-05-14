"""
W-3A Orchestrator Service

Complete reusable W-3A generation orchestration that can be called from:
1. w3a_from_api.py (user-initiated via REST endpoint)
2. w3_from_pna.py (auto-triggered when pnaexchange sends W-3 events)

This preserves ALL logic from W3AFromApiView.post() in a reusable function.

Flow:
  1. Request validation & API normalization
  2. Document acquisition (RRC extraction or user uploads)
  3. WellRegistry ensurance & enrichment
  4. GAU validity check & override handling
  5. Plan building (one or more variants)
  6. PlanSnapshot persistence
  7. Well engagement tracking
  8. Response formatting

See W3A_ORCHESTRATION_ANALYSIS.md for detailed flow documentation.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import uuid as _uuid

from django.db import transaction
from django.conf import settings
from django_tenants.utils import get_public_schema_name

from apps.public_core.serializers.w3a_plan import (
    W3APlanSerializer,
    W3APlanVariantsSerializer,
)
from apps.public_core.services.rrc_completions_extractor import extract_completions_all_documents
from apps.public_core.services.openai_extraction import classify_document, extract_json_from_pdf, vectorize_extracted_document
from apps.public_core.models import ExtractedDocument, WellRegistry, PlanSnapshot
from apps.public_core.services.well_registry_enrichment import enrich_well_registry_from_documents
from apps.tenant_overlay.models import TenantArtifact, WellEngagement
from apps.tenant_overlay.services.engagement_tracker import track_well_interaction
from apps.policy.services.loader import get_effective_policy
from apps.kernel.services.policy_kernel import plan_from_facts
from apps.kernel.services.jurisdiction_registry import detect_jurisdiction as _detect_jurisdiction

logger = logging.getLogger(__name__)


def _tenant_uuid_for_write(request):
    """Return the business tenant UUID for persisting records, excluding public schema."""
    if not (request and hasattr(request, 'user') and request.user.is_authenticated
            and request.user.tenants.exists()):
        return None
    public_schema = get_public_schema_name()
    tenant = request.user.tenants.exclude(schema_name=public_schema).first()
    return _uuid.UUID(int=tenant.pk) if tenant else None


def fetch_nm_extraction_data(api_number: str) -> Dict[str, Any]:
    """
    Fetch NM well data via scraper, format as extraction-like dict.

    This is the NM equivalent of extract_completions_all_documents() for TX.
    Instead of downloading and extracting PDFs, we scrape the NM OCD portal
    and get document metadata.

    Args:
        api_number: NM API number (e.g., "30-015-28692")

    Returns:
        {
            "well_data": dict from NMWellData.to_dict(),
            "documents": list of document metadata dicts,
            "combined_pdf_url": str URL to combined PDF on NM OCD,
            "extraction": dict in W-2-like format from mapper,
        }
    """
    from apps.public_core.services.nm_well_scraper import fetch_nm_well
    from apps.public_core.services.nm_document_fetcher import NMDocumentFetcher
    from apps.public_core.services.nm_extraction_mapper import (
        map_nm_well_to_extractions,
        create_nm_extracted_document_data,
    )

    logger.info(f"🆕 NM: Fetching well data for API {api_number}")

    # Scrape well data from NM OCD portal
    well_data = fetch_nm_well(api_number)
    well_dict = well_data.to_dict()

    logger.info(f"   ✅ Scraped well: {well_dict.get('well_name', 'Unknown')}")
    logger.info(f"   📍 Operator: {well_dict.get('operator_name', 'Unknown')}")
    logger.info(f"   📍 Status: {well_dict.get('status', 'Unknown')}")

    # Get document list from NM OCD imaging portal
    documents = []
    combined_pdf_url = None
    try:
        with NMDocumentFetcher() as fetcher:
            doc_list = fetcher.list_documents(api_number)
            documents = [
                {
                    "filename": d.filename,
                    "url": d.url,
                    "file_size": d.file_size,
                    "date": d.date,
                    "doc_type": d.doc_type,
                }
                for d in doc_list
            ]
            combined_pdf_url = fetcher.get_combined_pdf_url(api_number)
            logger.info(f"   📄 Found {len(documents)} documents in well file")
    except Exception as e:
        logger.warning(f"   ⚠️ Could not fetch document list: {e}")

    # Map to extraction format
    extraction = map_nm_well_to_extractions(well_dict)

    return {
        "well_data": well_dict,
        "documents": documents,
        "combined_pdf_url": combined_pdf_url,
        "extraction": extraction,
        "status": "success",
        "source": "nm_ocd_scraper",
    }


def generate_w3a_for_api(
    api_number: str,
    plugs_mode: str = "combined",
    input_mode: str = "extractions",
    merge_threshold_ft: float = 500.0,
    request=None,
    confirm_fact_updates: bool = False,
    allow_precision_upgrades_only: bool = True,
    use_gau_override_if_invalid: bool = False,
    gau_file=None,
    w2_file=None,
    w15_file=None,
    schematic_file=None,
    formation_tops_file=None,
    jurisdiction: str = None,
    workspace=None,
) -> Dict[str, Any]:
    """
    Generate complete W-3A plan with all extractions and enrichment.

    This is the core orchestration function that can be called from multiple endpoints.
    It preserves ALL logic from w3a_from_api.py W3AFromApiView.post() method.

    Supports both TX (RRC) and NM (OCD) jurisdictions:
    - TX: Uses RRC document extraction and OpenAI PDF processing
    - NM: Uses web scraper for well data + optional user file uploads

    Args:
        api_number: API number (10-digit format, e.g., "42-501-70575" for TX, "30-015-28692" for NM)
        plugs_mode: "combined", "isolated", or "both" (generate variants)
        input_mode: "extractions" (RRC/OCD), "user_files", or "hybrid"
        merge_threshold_ft: Threshold for long plug merging
        request: HTTP request for tenant/user context
        confirm_fact_updates: Apply WellRegistry updates from extracted data?
        allow_precision_upgrades_only: Conservative update policy?
        use_gau_override_if_invalid: Accept user GAU if RRC version invalid?
        gau_file, w2_file, w15_file, schematic_file, formation_tops_file: User uploads
        jurisdiction: Explicit jurisdiction ("TX" or "NM"). Auto-detected from API if not specified.
    
    Returns:
        {
            "success": bool,
            "w3a_data": {  # If success=true, the full plan output
                "variants": {...} or single plan dict
            },
            "snapshot_id": str,  # UUID for linking to W-3
            "auto_generated": bool,
            "extraction_count": int,
            "well_enriched": bool,
            "error": str,  # If success=false
            "validation": {
                "warnings": [],
                "errors": []
            }
        }
    """
    logger.info("=" * 80)
    logger.info("🚀 W-3A ORCHESTRATOR - Starting generation for API: %s", api_number)
    logger.info("=" * 80)
    
    warnings = []
    errors = []
    created: List[Dict[str, Any]] = []
    uploaded_refs: List[Dict[str, Any]] = []
    snapshot_id = None
    well_enriched = False
    
    try:
        # ============================================================
        # STEP 1: API NORMALIZATION
        # ============================================================
        logger.info("\n📍 STEP 1: Normalizing API number...")
        
        def _normalize_api(val: str) -> str:
            """Normalize various API formats to standard canonical form."""
            s = re.sub(r"\D+", "", str(val or ""))
            if len(s) in (14, 10, 8):
                return s
            if len(s) > 8:
                return s[-14:] if len(s) >= 14 else s[-10:] if len(s) >= 10 else s[-8:]
            return s
        
        api_in = _normalize_api(api_number)
        if not api_in:
            raise ValueError(f"Invalid API number format: {api_number}")
        logger.info(f"   ✅ Normalized API: {api_number} → {api_in}")

        # ============================================================
        # STEP 1.5: JURISDICTION DETECTION
        # ============================================================
        detected_jurisdiction = _detect_jurisdiction(api_in, jurisdiction)
        logger.info(f"   🗺️  Jurisdiction: {detected_jurisdiction}")

        # For NM wells, use the NM-specific flow
        if detected_jurisdiction == "NM":
            return _generate_w3a_for_nm_api(
                api_number=api_in,
                plugs_mode=plugs_mode,
                input_mode=input_mode,
                merge_threshold_ft=merge_threshold_ft,
                request=request,
                confirm_fact_updates=confirm_fact_updates,
                w2_file=w2_file,
                w15_file=w15_file,
                schematic_file=schematic_file,
                formation_tops_file=formation_tops_file,
                workspace=workspace,
            )

        # Continue with TX (RRC) flow below...

        # ============================================================
        # STEP 2: DOCUMENT ACQUISITION (TX/RRC)
        # ============================================================
        logger.info("\n📄 STEP 2: Acquiring documents...")
        
        def _ensure_dir(p: str) -> None:
            """Ensure directory exists."""
            os.makedirs(p, exist_ok=True)
        
        def _sha256_bytes(bts: bytes) -> str:
            """Compute SHA256 hash of bytes."""
            import hashlib
            h = hashlib.sha256()
            h.update(bts)
            return h.hexdigest()
        
        def _sha256_file(path: str) -> str:
            """Compute SHA256 hash of file."""
            import hashlib
            h = hashlib.sha256()
            with open(path, 'rb') as fp:
                for chunk in iter(lambda: fp.read(8192), b''):
                    h.update(chunk)
            return h.hexdigest()
        
        def _save_upload(fobj, api_digits: str) -> str:
            """Save uploaded file to media directory."""
            root = getattr(settings, "MEDIA_ROOT", ".")
            ts = str(int(__import__("time").time()))
            base_dir = os.path.join(root, "uploads", api_digits)
            _ensure_dir(base_dir)
            fname = getattr(fobj, "name", "upload.bin")
            safe_name = os.path.basename(fname)
            dest = os.path.join(base_dir, f"{ts}__{safe_name}")
            with open(dest, "wb") as outfp:
                chunk = fobj.read()
                outfp.write(chunk if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode("utf-8"))
            return dest
        
        def _detect_doc_type_from_json(obj: Dict[str, Any]) -> Optional[str]:
            """Auto-detect document type from JSON structure."""
            try:
                if isinstance(obj, dict):
                    if isinstance(obj.get("surface_casing_determination"), dict):
                        return "gau"
                    if "casing_record" in obj or "well_info" in obj:
                        return "w2"
                    if "cementing_report" in obj or "squeeze_operations" in obj:
                        return "w15"
                    if "schematic" in obj or "strings" in obj:
                        return "schematic"
                    if "formation_record" in obj:
                        return "formation_tops"
            except Exception:
                return None
            return None
        
        # Get or initialize well
        well = WellRegistry.objects.filter(api14__icontains=str(api_in)[-8:]).first()
        
        # Phase 2a: RRC Document Extraction
        dl: Dict[str, Any] = {}
        files: List[str] = []
        api = api_in
        if input_mode in ("extractions", "hybrid"):
            logger.info("   Extracting RRC documents...")
            print("\n" + "="*80, file=sys.stderr)
            print("📄 RRC DOCUMENT EXTRACTION STARTING", file=sys.stderr)
            print("="*80, file=sys.stderr)
            print(f"API: {api_in}", file=sys.stderr)
            print(f"Document types requested: w2, w15, gau", file=sys.stderr)
            print(f"Extraction mode: {input_mode}", file=sys.stderr)
            
            dl = extract_completions_all_documents(api_in, allowed_kinds=["w2", "w15", "gau"])
            files = dl.get("files") or []
            api = dl.get("api") or api_in
            
            print(f"\n📊 EXTRACTION RESULT:", file=sys.stderr)
            print(f"   Status: {dl.get('status')}", file=sys.stderr)
            print(f"   Source: {dl.get('source')}", file=sys.stderr)
            print(f"   Output directory: {dl.get('output_dir')}", file=sys.stderr)
            print(f"   Total files found: {len(files)}", file=sys.stderr)
            
            if files:
                print(f"\n📥 FILES DOWNLOADED:", file=sys.stderr)
                for i, f in enumerate(files, 1):
                    print(f"   [{i}] Name: {f.get('name')}", file=sys.stderr)
                    print(f"       Type: {f.get('content_type')}", file=sys.stderr)
                    print(f"       Size: {f.get('size_bytes', 0):,} bytes", file=sys.stderr)
                    print(f"       Path: {f.get('path')}", file=sys.stderr)
            else:
                print(f"\n❌ NO FILES DOWNLOADED", file=sys.stderr)
                if dl.get('status') != 'success':
                    print(f"   Reason: {dl.get('status')}", file=sys.stderr)
            
            print("="*80 + "\n", file=sys.stderr)
            logger.info(f"   ✅ RRC extraction: {len(files)} files found")
            
            print("\n" + "="*80, file=sys.stderr)
            print("🔄 PROCESSING RRC DOCUMENTS", file=sys.stderr)
            print("="*80, file=sys.stderr)
            
            for idx, f in enumerate(files, 1):
                path = f.get("path")
                if not path:
                    print(f"\n⏭️  [{idx}] SKIPPED: No path found", file=sys.stderr)
                    continue
                
                print(f"\n📖 [{idx}] PROCESSING: {f.get('name')}", file=sys.stderr)
                print(f"    Path: {path}", file=sys.stderr)
                
                try:
                    doc_type = classify_document(Path(path))
                    print(f"    Classified as: {doc_type}", file=sys.stderr)
                    
                    if doc_type not in ("gau", "w2", "w15", "schematic", "formation_tops"):
                        print(f"    ⏭️  SKIPPED: Document type '{doc_type}' not in allowed list", file=sys.stderr)
                        continue
                    
                    # --- Check for existing successful extraction (cache hit) ---
                    existing = ExtractedDocument.objects.filter(
                        api_number=api,
                        source_path=str(path),
                        document_type=doc_type,
                        status="success",
                        is_stale=False,
                    ).order_by("-created_at").first()

                    if existing:
                        logger.info(
                            "♻️  Reusing existing extraction for %s (ID: %s)",
                            os.path.basename(str(path)),
                            existing.id,
                        )
                        print(f"    ♻️  Cache hit — reusing ExtractedDocument: {existing.id}", file=sys.stderr)
                        ed = existing
                    else:
                        ext = extract_json_from_pdf(Path(path), doc_type)
                        print(f"    Extraction model: {ext.model_tag}", file=sys.stderr)

                        if ext.errors:
                            print(f"    ⚠️  Extraction errors: {ext.errors}", file=sys.stderr)

                        with transaction.atomic():
                            ed = ExtractedDocument.objects.create(
                                well=well,
                                api_number=api,
                                document_type=doc_type,
                                source_path=str(path),
                                model_tag=ext.model_tag,
                                status="success" if not ext.errors else "error",
                                errors=ext.errors,
                                json_data=ext.json_data,
                            )
                            print(f"    ✅ Created ExtractedDocument: {ed.id}", file=sys.stderr)
                            print(f"       Document Type: {ed.document_type}", file=sys.stderr)
                            print(f"       Status: {ed.status}", file=sys.stderr)

                            try:
                                vectorize_extracted_document(ed)
                                print(f"    ✅ Vectorization successful", file=sys.stderr)
                            except Exception as vec_err:
                                print(f"    ⚠️  Vectorization failed (non-fatal): {vec_err}", file=sys.stderr)
                                logger.exception("vectorize: failed for RRC doc")

                    created.append({"document_type": doc_type, "extracted_document_id": str(ed.id)})
                    print(f"    ✅ SUCCESS: Document processed and stored", file=sys.stderr)
                
                except Exception as e:
                    print(f"    ❌ FAILED: {type(e).__name__}: {e}", file=sys.stderr)
                    logger.warning(f"Failed to process RRC file {path}: {e}")
                    warnings.append(f"Failed to extract RRC {doc_type if 'doc_type' in locals() else 'unknown'}: {e}")
            
            print("\n" + "="*80, file=sys.stderr)
            print(f"📊 EXTRACTION PROCESSING SUMMARY", file=sys.stderr)
            print(f"   Total files to process: {len(files)}", file=sys.stderr)
            print(f"   Successfully processed: {len(created)}", file=sys.stderr)
            print(f"   Failed: {len(files) - len(created)}", file=sys.stderr)
            print("="*80 + "\n", file=sys.stderr)
        
        # Phase 2b: User File Upload Ingestion
        if input_mode in ("user_files", "hybrid"):
            logger.info("   Processing user file uploads...")
            print("\n" + "="*80, file=sys.stderr)
            print("📤 USER FILE UPLOAD PROCESSING", file=sys.stderr)
            print("="*80, file=sys.stderr)
            
            uploads = [("w2", w2_file), ("w15", w15_file), ("gau", gau_file), ("schematic", schematic_file), ("formation_tops", formation_tops_file)]
            uploads_provided = sum(1 for _, f in uploads if f is not None)
            print(f"Files provided: {uploads_provided}", file=sys.stderr)
            
            for label, fobj in uploads:
                if not fobj:
                    print(f"\n⏭️  {label.upper()}: Not provided", file=sys.stderr)
                    continue
                
                print(f"\n📥 Processing {label.upper()} upload", file=sys.stderr)
                
                try:
                    content_type = getattr(fobj, "content_type", "") or ""
                    filename = getattr(fobj, "name", "") or ""
                    is_json = ("json" in content_type.lower()) or filename.lower().endswith(".json")
                    print(f"   Filename: {filename}", file=sys.stderr)
                    print(f"   Content type: {content_type}", file=sys.stderr)
                    print(f"   Format: {'JSON' if is_json else 'PDF'}", file=sys.stderr)
                    
                    if is_json:
                        raw = fobj.read()
                        data = json.loads(raw.decode("utf-8")) if isinstance(raw, (bytes, bytearray)) else json.loads(str(raw))
                        doc_type = _detect_doc_type_from_json(data) or label
                        with transaction.atomic():
                            ed = ExtractedDocument.objects.create(
                                well=well,
                                api_number=api,
                                document_type=doc_type,
                                source_path=f"upload:{filename or 'user.json'}",
                                model_tag="user_uploaded_json",
                                status="success",
                                errors=[],
                                json_data=data,
                            )
                        try:
                            vectorize_extracted_document(ed)
                        except Exception:
                            logger.exception("vectorize: failed for user JSON")
                            try:
                                digest = _sha256_bytes(raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode("utf-8"))
                                TenantArtifact.objects.create(
                                    artifact_type=doc_type,
                                    file_path=f"upload:{filename or 'user.json'}",
                                    content_type=content_type or "application/json",
                                    size_bytes=len(raw) if isinstance(raw, (bytes, bytearray)) else len(str(raw).encode("utf-8")),
                                    sha256=digest,
                                    extracted_document=ed,
                                    plan_snapshot=None,
                                    metadata={"source": "user_upload", "label": label},
                                )
                            except Exception:
                                logger.exception("Failed to persist TenantArtifact for JSON upload")
                        created.append({"document_type": doc_type, "extracted_document_id": str(ed.id)})
                        uploaded_refs.append({"type": doc_type, "filename": filename or "user.json", "kind": "json"})
                    else:
                        # PDF upload
                        saved_path = _save_upload(fobj, str(api))
                        doc_type = classify_document(Path(saved_path))
                        if doc_type not in ("gau", "w2", "w15", "schematic", "formation_tops"):
                            continue
                        # --- Check for existing successful extraction (cache hit) ---
                        existing = ExtractedDocument.objects.filter(
                            api_number=api,
                            source_path=str(saved_path),
                            document_type=doc_type,
                            status="success",
                            is_stale=False,
                        ).order_by("-created_at").first()

                        if existing:
                            logger.info(
                                "♻️  Reusing existing extraction for %s (ID: %s)",
                                os.path.basename(str(saved_path)),
                                existing.id,
                            )
                            print(f"   ♻️  Cache hit — reusing ExtractedDocument: {existing.id}", file=sys.stderr)
                            ed = existing
                        else:
                            ext = extract_json_from_pdf(Path(saved_path), doc_type)
                            with transaction.atomic():
                                ed = ExtractedDocument.objects.create(
                                    well=well,
                                    api_number=api,
                                    document_type=doc_type,
                                    source_path=saved_path,
                                    model_tag="user_uploaded_pdf",
                                    status="success" if not ext.errors else "error",
                                    errors=ext.errors,
                                    json_data=ext.json_data,
                                )
                            try:
                                vectorize_extracted_document(ed)
                            except Exception:
                                logger.exception("vectorize: failed for user PDF")
                                try:
                                    size_bytes = None
                                    try:
                                        size_bytes = os.path.getsize(saved_path)
                                    except Exception:
                                        size_bytes = None
                                    digest = _sha256_file(saved_path)
                                    TenantArtifact.objects.create(
                                        artifact_type=doc_type,
                                        file_path=saved_path,
                                        content_type=content_type or "application/pdf",
                                        size_bytes=size_bytes,
                                        sha256=digest,
                                        extracted_document=ed,
                                        plan_snapshot=None,
                                        metadata={"source": "user_upload", "label": label},
                                    )
                                except Exception:
                                    logger.exception("Failed to persist TenantArtifact for PDF upload")
                        created.append({"document_type": doc_type, "extracted_document_id": str(ed.id)})
                        uploaded_refs.append({"type": doc_type, "filename": os.path.basename(saved_path), "kind": "pdf"})
                        print(f"   ✅ SUCCESS: {label.upper()} upload processed", file=sys.stderr)
                except Exception as e:
                    print(f"   ❌ FAILED: {label.upper()} upload - {type(e).__name__}: {e}", file=sys.stderr)
                    logger.warning(f"Failed to process user upload {label}: {e}")
                    warnings.append(f"Failed to ingest {label} upload: {e}")
            
            print("\n" + "="*80, file=sys.stderr)
            print(f"📊 USER UPLOADS PROCESSING SUMMARY", file=sys.stderr)
            print(f"   Total uploads provided: {uploads_provided}", file=sys.stderr)
            print(f"   Successfully processed: {len([u for u in uploaded_refs if u.get('kind')])}", file=sys.stderr)
            print("="*80 + "\n", file=sys.stderr)
        
        logger.info(f"   ✅ Document acquisition complete: {len(created)} extractions")
        
        # Print comprehensive document extraction summary
        print("\n" + "="*80, file=sys.stderr)
        print("📋 DOCUMENT ACQUISITION PHASE COMPLETE", file=sys.stderr)
        print("="*80, file=sys.stderr)
        print(f"Input mode: {input_mode}", file=sys.stderr)
        print(f"Total ExtractedDocuments created: {len(created)}", file=sys.stderr)
        
        if created:
            print(f"\n✅ Successfully extracted {len(created)} document(s):", file=sys.stderr)
            for i, doc in enumerate(created, 1):
                print(f"   [{i}] Type: {doc.get('document_type')}", file=sys.stderr)
                print(f"       ID: {doc.get('extracted_document_id')}", file=sys.stderr)
        else:
            print(f"\n❌ NO DOCUMENTS EXTRACTED!", file=sys.stderr)
            print(f"   RRC extraction status: {dl.get('status')}", file=sys.stderr)
            print(f"   Files found: {len(files)}", file=sys.stderr)
            print(f"   Uploads processed: {uploads_provided if input_mode in ('user_files', 'hybrid') else 0}", file=sys.stderr)
        
        print(f"\nWarnings: {len(warnings)}", file=sys.stderr)
        if warnings:
            for w in warnings:
                print(f"   ⚠️  {w}", file=sys.stderr)
        
        print("="*80 + "\n", file=sys.stderr)
        
        # ============================================================
        # STEP 3: WELLREGISTRY ENSURANCE & ENRICHMENT
        # ============================================================
        logger.info("\n🏛️ STEP 3: Ensuring WellRegistry and enriching...")
        
        try:
            api_digits = re.sub(r"\D+", "", str(api or ""))
            api_candidate = api_digits or api_in
            
            # Fetch latest extractions for enrichment
            w2_latest = (
                ExtractedDocument.objects
                .filter(api_number=api, document_type="w2")
                .order_by("-created_at")
                .first()
            )
            gau_latest = (
                ExtractedDocument.objects
                .filter(api_number=api, document_type="gau")
                .order_by("-created_at")
                .first()
            )
            
            # Extract well metadata from latest documents
            api14_cand = None
            county_cand = None
            district_cand = None
            lat_cand = None
            lon_cand = None
            operator_cand = None
            field_cand = None
            
            if w2_latest and isinstance(w2_latest.json_data, dict):
                wi = (w2_latest.json_data.get("well_info") or {})
                api14_cand = re.sub(r"\D+", "", str(wi.get("api") or "")) or None
                county_cand = wi.get("county") or None
                district_cand = wi.get("district") or None
                loc = wi.get("location") or {}
                lat_cand = loc.get("lat") or loc.get("latitude") or None
                lon_cand = loc.get("lon") or loc.get("longitude") or None
                operator_cand = wi.get("operator") or wi.get("operator_name") or None
                field_cand = wi.get("field") or wi.get("field_name") or None
            
            # Fallback to GAU coordinates
            if (lat_cand is None or lon_cand is None) and gau_latest and isinstance(gau_latest.json_data, dict):
                wi_g = (gau_latest.json_data.get("well_info") or {})
                loc_g = wi_g.get("location") or {}
                lat_cand = lat_cand or loc_g.get("lat") or loc_g.get("latitude") or wi_g.get("latitude")
                lon_cand = lon_cand or loc_g.get("lon") or loc_g.get("longitude") or wi_g.get("longitude")
            
            api14_final = api14_cand or re.sub(r"\D+", "", str(api_candidate or ""))
            proposed_changes: Dict[str, Any] = {}
            applied_changes: Dict[str, Any] = {}
            
            if api14_final:
                well, _created = WellRegistry.objects.get_or_create(
                    api14=api14_final,
                    defaults={"state": "TX", "county": (county_cand or "")},
                )
                
                # Stage fact updates
                if county_cand and not (well.county or "").strip():
                    proposed_changes["county"] = {"before": well.county, "after": str(county_cand), "source": "w2"}
                if district_cand and not (well.district or "").strip():
                    proposed_changes["district"] = {"before": well.district, "after": str(district_cand), "source": "w2"}
                if operator_cand and not (well.operator_name or "").strip():
                    proposed_changes["operator_name"] = {"before": well.operator_name, "after": str(operator_cand)[:128], "source": "w2"}
                if field_cand and not (well.field_name or "").strip():
                    proposed_changes["field_name"] = {"before": well.field_name, "after": str(field_cand)[:128], "source": "w2"}
                
                # Stage coordinate updates
                if lat_cand is not None and lon_cand is not None:
                    before_lat = float(well.lat) if well.lat is not None else None
                    before_lon = float(well.lon) if well.lon is not None else None
                    proposed_changes["lat"] = {"before": before_lat, "after": float(lat_cand), "source": ("w2" if w2_latest else "gau")}
                    proposed_changes["lon"] = {"before": before_lon, "after": float(lon_cand), "source": ("w2" if w2_latest else "gau")}
                
                # Apply updates if confirmed
                if proposed_changes and confirm_fact_updates:
                    def _small_delta(old_val, new_val):
                        try:
                            if old_val is None:
                                return True
                            return abs(float(old_val) - float(new_val)) < 0.001
                        except Exception:
                            return False
                    
                    # Apply text fields
                    for field in ["county", "district", "operator_name", "field_name"]:
                        if field in proposed_changes and (not (getattr(well, field) or "").strip() or not allow_precision_upgrades_only):
                            setattr(well, field, proposed_changes[field]["after"])
                            applied_changes[field] = proposed_changes[field]
                    
                    # Apply coordinates
                    if "lat" in proposed_changes and "lon" in proposed_changes:
                        new_lat = proposed_changes["lat"]["after"]
                        new_lon = proposed_changes["lon"]["after"]
                        if not allow_precision_upgrades_only:
                            well.lat = new_lat
                            well.lon = new_lon
                            applied_changes["lat"] = proposed_changes["lat"]
                            applied_changes["lon"] = proposed_changes["lon"]
                        else:
                            if (well.lat is None or well.lon is None) or (_small_delta(well.lat, new_lat) and _small_delta(well.lon, new_lon)):
                                well.lat = new_lat
                                well.lon = new_lon
                                applied_changes["lat"] = proposed_changes["lat"]
                                applied_changes["lon"] = proposed_changes["lon"]
                    
                    if applied_changes:
                        well.save()
                
                # Backfill ExtractedDocuments to well
                try:
                    ed_ids = [c.get("extracted_document_id") for c in created if c.get("extracted_document_id")]
                    if ed_ids:
                        ExtractedDocument.objects.filter(id__in=ed_ids).update(well=well)
                except Exception:
                    logger.exception("Failed to backfill well on ExtractedDocuments")
        
        except Exception:
            logger.exception("Failed to ensure WellRegistry before plan build")
        
        # Enrich well from extracted documents
        if well:
            try:
                extracted_docs = ExtractedDocument.objects.filter(well=well, api_number__contains=str(api)[-8:])
                if extracted_docs.exists():
                    enrich_well_registry_from_documents(well, list(extracted_docs))
                    well_enriched = True
                    logger.info(f"✅ Enriched WellRegistry from {extracted_docs.count()} documents")
            except Exception:
                logger.exception(f"Failed to enrich WellRegistry (non-fatal)")
        
        logger.info("   ✅ WellRegistry ready")
        
        # ============================================================
        # STEP 4: GAU VALIDITY CHECK & OVERRIDE
        # ============================================================
        logger.info("\n🔍 STEP 4: GAU validity check...")
        
        def _has_valid_gau(api_check: str) -> bool:
            """GAU valid if <= 5 years old and has determination depth."""
            try:
                gau_doc = (
                    ExtractedDocument.objects
                    .filter(api_number=api_check, document_type="gau")
                    .order_by("-created_at")
                    .first()
                )
                gau = (gau_doc and gau_doc.json_data) or {}
                import datetime as _dt
                gau_date_txt = ((gau.get("header") or {}).get("date") if gau else None) or None
                gau_depth = (gau.get("surface_casing_determination") or {}).get("gau_groundwater_protection_determination_depth") if gau else None
                if gau_depth is None or not gau_date_txt:
                    return False
                gau_dt = None
                for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%Y/%m/%d", "%d %B %Y", "%B %d, %Y", "%d %b %Y", "%b %d, %Y"):
                    try:
                        gau_dt = _dt.datetime.strptime(str(gau_date_txt), fmt)
                        break
                    except Exception:
                        gau_dt = None
                if not gau_dt:
                    return False
                age_days = (_dt.datetime.utcnow() - gau_dt).days
                return bool(age_days <= (5 * 365))
            except Exception:
                return False
        
        gau_valid = _has_valid_gau(api)
        if input_mode == "extractions" and (not gau_valid) and use_gau_override_if_invalid and gau_file is not None:
            logger.info("   Processing GAU override...")
            content_type = getattr(gau_file, "content_type", None) or ""
            filename = getattr(gau_file, "name", "") or ""
            is_json = ("json" in content_type.lower()) or filename.lower().endswith(".json")
            try:
                if is_json:
                    raw = gau_file.read()
                    data = json.loads(raw.decode("utf-8")) if isinstance(raw, (bytes, bytearray)) else json.loads(str(raw))
                    with transaction.atomic():
                        ExtractedDocument.objects.create(
                            well=well,
                            api_number=api,
                            document_type="gau",
                            source_path=filename or "gau(user).json",
                            model_tag="user_uploaded_json",
                            status="success",
                            errors=[],
                            json_data=data,
                        )
                else:
                    tmp_path = _persist_upload_to_tmp_pdf(gau_file)
                    try:
                        # --- Check for existing successful extraction (cache hit) ---
                        existing = ExtractedDocument.objects.filter(
                            api_number=api,
                            source_path=str(tmp_path),
                            document_type="gau",
                            status="success",
                            is_stale=False,
                        ).order_by("-created_at").first()

                        if existing:
                            logger.info(
                                "♻️  Reusing existing extraction for %s (ID: %s)",
                                os.path.basename(str(tmp_path)),
                                existing.id,
                            )
                        else:
                            ext = extract_json_from_pdf(Path(tmp_path), "gau")
                            with transaction.atomic():
                                ExtractedDocument.objects.create(
                                    well=well,
                                    api_number=api,
                                    document_type="gau",
                                    source_path=tmp_path,
                                    model_tag=ext.model_tag,
                                    status="success" if not ext.errors else "error",
                                    errors=ext.errors,
                                    json_data=ext.json_data,
                                )
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except Exception:
                            pass
                logger.info("   ✅ GAU override ingested")
            except Exception as e:
                logger.exception("GAU JSON override ingest failed")
                warnings.append(f"GAU override failed: {e}")
        
        # ============================================================
        # STEP 5: PLAN BUILDING
        # ============================================================
        logger.info("\n🏗️ STEP 5: Building W-3A plan variants...")
        
        extraction_info = {
            "status": dl.get("status"),
            "source": dl.get("source"),
            "output_dir": dl.get("output_dir"),
            "files": [os.fspath((f or {}).get("path")) for f in (dl.get("files") or [])],
        }
        if uploaded_refs:
            extraction_info["user_files"] = uploaded_refs
        
        try:
            if plugs_mode == "both":
                combined = _build_plan_helper(api, merge_enabled=True, merge_threshold_ft=merge_threshold_ft)
                isolated = _build_plan_helper(api, merge_enabled=False, merge_threshold_ft=merge_threshold_ft)
                plan_output = {"variants": {"combined": combined, "isolated": isolated}, "extraction": extraction_info}
            else:
                merge_enabled = (plugs_mode == "combined")
                plan = _build_plan_helper(api, merge_enabled=merge_enabled, merge_threshold_ft=merge_threshold_ft)
                plan_output = {**plan, "extraction": extraction_info}
            
            logger.info("   ✅ Plan building complete")
        except Exception as e:
            logger.error(f"Plan building failed: {e}", exc_info=True)
            raise
        
        # ============================================================
        # STEP 6: PLANSNAPSHOT PERSISTENCE
        # ============================================================
        logger.info("\n💾 STEP 6: Persisting PlanSnapshot...")
        
        try:
            well_for_snapshot = well or WellRegistry.objects.filter(api14__icontains=str(api)[-8:]).first()
            if well_for_snapshot is not None:
                if plugs_mode == "both":
                    plan_id = f"{api}:both"
                    payload = plan_output
                    variant_label = "both"
                else:
                    variant_label = "combined" if plugs_mode == "combined" else "isolated"
                    plan_id = f"{api}:{variant_label}"
                    payload = plan_output
                
                snapshot = PlanSnapshot.objects.create(
                    well=well_for_snapshot,
                    plan_id=plan_id,
                    kind=PlanSnapshot.KIND_BASELINE,
                    payload=payload,
                    kernel_version=str((payload.get("variants", {}).get("combined") or payload).get("kernel_version") or ""),
                    policy_id="tx.w3a",
                    overlay_id="",
                    extraction_meta=extraction_info,
                    visibility=PlanSnapshot.VISIBILITY_PUBLIC,
                    tenant_id=_tenant_uuid_for_write(request),
                    workspace=workspace,
                    status=PlanSnapshot.STATUS_DRAFT,
                )
                snapshot_id = str(snapshot.id)
                
                # Link artifacts to snapshot
                try:
                    ed_ids = [c.get("extracted_document_id") for c in created if c.get("extracted_document_id")]
                    if ed_ids:
                        TenantArtifact.objects.filter(extracted_document__id__in=ed_ids).update(plan_snapshot=snapshot)
                except Exception:
                    logger.exception("Failed to link TenantArtifacts")
                
                # Track well engagement
                try:
                    if request and hasattr(request, 'user') and request.user.is_authenticated:
                        user_tenant = request.user.tenants.first()
                        if user_tenant:
                            track_well_interaction(
                                tenant_id=user_tenant.id,
                                well=well_for_snapshot,
                                interaction_type=WellEngagement.InteractionType.W3A_GENERATED,
                                user=request.user,
                                metadata_update={
                                    'plan_id': plan_id,
                                    'snapshot_id': snapshot_id,
                                    'plugs_mode': plugs_mode,
                                    'auto_generated': True
                                }
                            )
                except Exception:
                    logger.exception("Failed to track well engagement")
                
                logger.info(f"   ✅ PlanSnapshot created: {snapshot_id}")

                # Write plan_proposed WellComponent records
                try:
                    from apps.public_core.services.component_writer import write_plan_components
                    if plugs_mode == "both":
                        steps = plan_output.get("variants", {}).get("combined", {}).get("steps", [])
                    else:
                        steps = plan_output.get("steps", [])
                    tenant_id_for_components = _tenant_uuid_for_write(request)
                    write_plan_components(
                        well=well_for_snapshot,
                        plan_snapshot=snapshot,
                        steps=steps,
                        tenant_id=tenant_id_for_components,
                    )
                except Exception:
                    logger.warning("Failed to write plan components", exc_info=True)

        except Exception as e:
            logger.exception("Failed to persist PlanSnapshot")
            warnings.append(f"Snapshot persistence failed: {e}")

        # ============================================================
        # STEP 7: EXTRACT WELL GEOMETRY
        # ============================================================
        logger.info("\n🏗️ STEP 7: Extracting well geometry...")
        
        well_geometry = extract_well_geometry_from_w3a(api)
        logger.info("   ✅ Well geometry extracted")
        
        # ============================================================
        # STEP 8: RESPONSE FORMATTING
        # ============================================================
        logger.info("\n📦 STEP 8: Formatting response...")
        
        return {
            "success": True,
            "w3a_data": plan_output,
            "w3a_well_geometry": well_geometry,
            "snapshot_id": snapshot_id,
            "auto_generated": True,
            "extraction_count": len(created),
            "well_enriched": well_enriched,
            "validation": {
                "warnings": warnings,
                "errors": errors
            }
        }
    
    except Exception as e:
        logger.error(f"❌ W-3A orchestration failed: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "w3a_data": None,
            "snapshot_id": None,
            "auto_generated": True,
            "extraction_count": len(created),
            "well_enriched": well_enriched,
            "validation": {
                "warnings": warnings,
                "errors": [str(e)] + errors
            }
        }


def _persist_upload_to_tmp_pdf(fobj) -> str:
    """Persist uploaded PDF to temporary file."""
    suffix = ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        chunk = fobj.read()
        if isinstance(chunk, bytes):
            tmp.write(chunk)
        else:
            tmp.write(chunk.encode("utf-8"))
        return tmp.name


def extract_well_geometry_from_w3a(api: str) -> Dict[str, Any]:
    """
    Extract well geometry and historical data from W-3A extractions.
    
    Returns only the data needed for plugged wellbore diagram:
    - Casing record (from W-2)
    - Existing tools (CIBP, packer, DV tool)
    - Retainer tools (float collar, pup joint, straddle packer, retainer)
    - Historic cement jobs (from W-15)
    - KOP data (for horizontal wells)
    
    This is called after W-3A generation to extract geometry for the W-3 response.
    
    Returns:
    {
        "casing_record": [...],
        "existing_tools": {
            "existing_mechanical_barriers": ["CIBP", "PACKER"],
            "existing_cibp_ft": 1500,
            "existing_packer_ft": 2000,
            "existing_dv_tool_ft": 1800
        },
        "retainer_tools": [...],
        "historic_cement_jobs": [...],
        "kop": {"kop_md_ft": 5000, "kop_tvd_ft": 4000}
    }
    """
    logger.info(f"Extracting well geometry from W-3A data for API: {api}")
    
    well_geometry = {
        "casing_record": [],
        "existing_tools": {},
        "retainer_tools": [],
        "historic_cement_jobs": [],
        "kop": None
    }
    
    try:
        # Get latest W-2 for casing record
        w2_doc = ExtractedDocument.objects.filter(api_number=api, document_type="w2").order_by("-created_at").first()
        if w2_doc and isinstance(w2_doc.json_data, dict):
            w2 = w2_doc.json_data
            
            # Extract casing record from W-2
            try:
                casing_record = w2.get("casing_record", [])
                if isinstance(casing_record, list):
                    well_geometry["casing_record"] = casing_record
                    logger.info(f"   ✅ Extracted {len(casing_record)} casing strings")
            except Exception as e:
                logger.warning(f"Failed to extract casing record: {e}")
            
            # Extract existing mechanical barriers from W-2 remarks
            try:
                remarks_txt = str(w2.get("remarks") or "")
                rrc_remarks_obj = w2.get("rrc_remarks") or {}
                rrc_remarks_txt = ""
                if isinstance(rrc_remarks_obj, dict):
                    for key, val in rrc_remarks_obj.items():
                        if val:
                            rrc_remarks_txt += f" {val}"
                elif isinstance(rrc_remarks_obj, str):
                    rrc_remarks_txt = rrc_remarks_obj
                
                combined_remarks = f"{remarks_txt} {rrc_remarks_txt}"
                
                existing_mech_barriers = []
                existing_cibp_ft = None
                existing_packer_ft = None
                existing_dv_tool_ft = None
                
                # Search for CIBP
                for pattern in [
                    r"CIBP\s*(?:at|@)?\s*(\d{3,5})",
                    r"cast\s*iron\s*bridge\s*plug\s*(?:at|@)?\s*(\d{3,5})",
                    r"\bBP\b\s*(?:at|@)?\s*(\d{3,5})"
                ]:
                    match = re.search(pattern, combined_remarks, flags=re.IGNORECASE)
                    if match:
                        try:
                            existing_cibp_ft = float(match.group(1))
                            if "CIBP" not in existing_mech_barriers:
                                existing_mech_barriers.append("CIBP")
                            break
                        except Exception:
                            pass
                
                # Search for Packer
                packer_match = re.search(r"packer\s*(?:at|set\s*at|@)?\s*(\d{3,5})", combined_remarks, flags=re.IGNORECASE)
                if packer_match:
                    try:
                        existing_packer_ft = float(packer_match.group(1))
                        if "PACKER" not in existing_mech_barriers:
                            existing_mech_barriers.append("PACKER")
                    except Exception:
                        pass
                
                # Search for DV tool
                for pattern in [
                    r"DV[- ]?(?:stage)?\s*tool\s*(?:at|@)?\s*(\d{3,5})",
                    r"DV[- ]?tool\s*(\d{3,5})"
                ]:
                    dv_match = re.search(pattern, combined_remarks, flags=re.IGNORECASE)
                    if dv_match:
                        try:
                            existing_dv_tool_ft = float(dv_match.group(1))
                            if "DV_TOOL" not in existing_mech_barriers:
                                existing_mech_barriers.append("DV_TOOL")
                            break
                        except Exception:
                            pass
                
                if existing_mech_barriers:
                    well_geometry["existing_tools"]["existing_mechanical_barriers"] = existing_mech_barriers
                if existing_cibp_ft is not None:
                    well_geometry["existing_tools"]["existing_cibp_ft"] = existing_cibp_ft
                if existing_packer_ft is not None:
                    well_geometry["existing_tools"]["existing_packer_ft"] = existing_packer_ft
                if existing_dv_tool_ft is not None:
                    well_geometry["existing_tools"]["existing_dv_tool_ft"] = existing_dv_tool_ft
                
                if well_geometry["existing_tools"]:
                    logger.info(f"   ✅ Extracted existing tools: {well_geometry['existing_tools']}")
            except Exception as e:
                logger.warning(f"Failed to extract existing tools: {e}")
            
            # Extract retainer tools from W-2 remarks
            try:
                retainer_tools = []
                
                # Search for Retainer
                for pattern in [
                    r"retainer\s*(?:at|@)?\s*(\d{3,5})",
                    r"retainer\s+(?:packer\s+)?(?:at|@)?\s*(\d{3,5})"
                ]:
                    retainer_matches = re.finditer(pattern, combined_remarks, flags=re.IGNORECASE)
                    for match in retainer_matches:
                        try:
                            depth = float(match.group(1))
                            retainer_tools.append({"tool_type": "retainer", "depth_ft": depth})
                        except Exception:
                            pass
                
                # Search for Straddle Packer
                for pattern in [
                    r"straddle\s*(?:packer\s+)?(?:at|@)?\s*(\d{3,5})",
                    r"straddle\s*(?:at|@)?\s*(\d{3,5})"
                ]:
                    straddle_matches = re.finditer(pattern, combined_remarks, flags=re.IGNORECASE)
                    for match in straddle_matches:
                        try:
                            depth = float(match.group(1))
                            if not any(t.get("tool_type") == "straddle_packer" and t.get("depth_ft") == depth for t in retainer_tools):
                                retainer_tools.append({"tool_type": "straddle_packer", "depth_ft": depth})
                        except Exception:
                            pass
                
                # Search for Float Collar
                for pattern in [
                    r"float\s*(?:collar\s+)?(?:at|@)?\s*(\d{3,5})",
                    r"float\s*(?:at|@)?\s*(\d{3,5})"
                ]:
                    float_matches = re.finditer(pattern, combined_remarks, flags=re.IGNORECASE)
                    for match in float_matches:
                        try:
                            depth = float(match.group(1))
                            if not any(t.get("tool_type") == "float_collar" and t.get("depth_ft") == depth for t in retainer_tools):
                                retainer_tools.append({"tool_type": "float_collar", "depth_ft": depth})
                        except Exception:
                            pass
                
                # Search for Pup Joint
                for pattern in [
                    r"pup\s*(?:joint\s+)?(?:at|@)?\s*(\d{3,5})",
                    r"pup\s*(?:at|@)?\s*(\d{3,5})"
                ]:
                    pup_matches = re.finditer(pattern, combined_remarks, flags=re.IGNORECASE)
                    for match in pup_matches:
                        try:
                            depth = float(match.group(1))
                            if not any(t.get("tool_type") == "pup_joint" and t.get("depth_ft") == depth for t in retainer_tools):
                                retainer_tools.append({"tool_type": "pup_joint", "depth_ft": depth})
                        except Exception:
                            pass
                
                well_geometry["retainer_tools"] = retainer_tools
                if retainer_tools:
                    logger.info(f"   ✅ Extracted {len(retainer_tools)} retainer tools")
            except Exception as e:
                logger.warning(f"Failed to extract retainer tools: {e}")
            
            # Extract KOP (Kick-Off Point) for horizontal wells
            try:
                kop_data = w2.get("kop") or {}
                if isinstance(kop_data, dict):
                    kop_md_ft = kop_data.get("kop_md_ft")
                    kop_tvd_ft = kop_data.get("kop_tvd_ft")
                    if kop_md_ft is not None:
                        kop_md_ft = float(kop_md_ft)
                    if kop_tvd_ft is not None:
                        kop_tvd_ft = float(kop_tvd_ft)
                    
                    if kop_md_ft is not None or kop_tvd_ft is not None:
                        well_geometry["kop"] = {
                            "kop_md_ft": kop_md_ft,
                            "kop_tvd_ft": kop_tvd_ft
                        }
                        logger.info(f"   ✅ Extracted KOP: MD={kop_md_ft} ft, TVD={kop_tvd_ft} ft")
            except Exception as e:
                logger.warning(f"Failed to extract KOP data: {e}")
    
    except Exception as e:
        logger.warning(f"Failed to extract W-2 data: {e}")
    
    # Get latest W-15 for historic cement jobs
    # Store all cement jobs without filtering to preserve complete historical data
    try:
        w15_doc = ExtractedDocument.objects.filter(api_number=api, document_type="w15").order_by("-created_at").first()
        if w15_doc and isinstance(w15_doc.json_data, dict):
            w15 = w15_doc.json_data
            
            historic_cement_jobs = []
            cementing_data = w15.get("cementing_data") or []
            if isinstance(cementing_data, list):
                for cement_job in cementing_data:
                    if isinstance(cement_job, dict):
                        try:
                            # Include all available fields from the cement job
                            job_entry = {
                                "job_type": cement_job.get("job"),
                                "interval_top_ft": cement_job.get("interval_top_ft"),
                                "interval_bottom_ft": cement_job.get("interval_bottom_ft"),
                                "cement_top_ft": cement_job.get("cement_top_ft"),
                                "sacks": cement_job.get("sacks"),
                                "slurry_density_ppg": cement_job.get("slurry_density_ppg"),
                                "additives": cement_job.get("additives"),
                                "yield_ft3_per_sk": cement_job.get("yield_ft3_per_sk"),
                            }
                            # Store all cement jobs as-is, preserving complete historical data
                            historic_cement_jobs.append(job_entry)
                        except Exception:
                            pass
            
            well_geometry["historic_cement_jobs"] = historic_cement_jobs
            if historic_cement_jobs:
                logger.info(f"   ✅ Extracted {len(historic_cement_jobs)} historic cement jobs from W-15")
    except Exception as e:
        logger.warning(f"Failed to extract W-15 data: {e}")
    
    logger.info("   ✅ Well geometry extraction complete")
    return well_geometry


def _build_plan_helper(api: str, *, merge_enabled: bool, merge_threshold_ft: float) -> Dict[str, Any]:
    """
    Build single W-3A plan variant.
    
    IMPORTANT: This function DELEGATES to W3AFromApiView._build_plan() to preserve
    the full 600+ lines of critical logic unchanged. This is a pragmatic approach
    to avoid code duplication while keeping the orchestrator lean.
    
    The plan-building logic requires deep knowledge of:
    - Latest extraction fetching and conditional validation
    - Well geometry extraction with multiple field name handling
    - GAU date parsing with multiple format support
    - Existing mechanical barriers regex parsing
    - KOP extraction for horizontal wells
    - Comprehensive facts dictionary building
    - Policy loading and preference configuration
    - Casing ID/stinger mapping
    - Kernel execution with facts/policy
    - Step formatting, RRC export, and depth normalization
    
    This is too intricate to duplicate without risk of logic divergence.
    
    Returns the result from W3AFromApiView._build_plan(api, merge_enabled, merge_threshold_ft)
    which constructs the complete plan output including:
    - api, jurisdiction, district, county, field
    - formation_tops_detected, formations_targeted
    - steps (with step_ids, normalized depths, materials, regulatory basis)
    - plan_notes, materials_totals
    - rrc_export (for RRC Form W-3 submission)
    - violations, kernel_version
    """
    logger.info(f"   Delegating to W3AFromApiView._build_plan(): merge={merge_enabled}, threshold={merge_threshold_ft}ft")

    # Import locally to avoid circular dependency
    from apps.public_core.views.w3a_from_api import W3AFromApiView

    # Create a view instance and call its _build_plan method
    # This preserves all the critical extraction logic
    view = W3AFromApiView()
    plan = view._build_plan(api, merge_enabled=merge_enabled, merge_threshold_ft=merge_threshold_ft)

    logger.info(f"   ✅ Plan built: {len(plan.get('steps', []))} steps")
    return plan


def _generate_w3a_for_nm_api(
    api_number: str,
    plugs_mode: str = "combined",
    input_mode: str = "extractions",
    merge_threshold_ft: float = 500.0,
    request=None,
    confirm_fact_updates: bool = False,
    w2_file=None,
    w15_file=None,
    schematic_file=None,
    formation_tops_file=None,
    workspace=None,
) -> Dict[str, Any]:
    """
    Generate W-3A plan for NM wells using scraped data.

    This is the NM-specific flow that:
    1. Auto-imports well to WellRegistry from NM OCD scraper
    2. Creates pseudo-extraction from scraped data
    3. Returns scraped data + document URLs for user review
    4. User can upload documents for manual review if needed

    Note: NM wells require more manual data entry since the NM OCD combined
    PDFs are too large (100+ pages) for automated extraction.

    Args:
        api_number: NM API number (normalized, e.g., "3001528692")
        plugs_mode: "combined", "isolated", or "both"
        input_mode: "extractions" (uses scraper), "user_files", or "hybrid"
        merge_threshold_ft: Threshold for long plug merging
        request: HTTP request for tenant/user context
        confirm_fact_updates: Apply WellRegistry updates from scraped data?
        w2_file, w15_file, schematic_file, formation_tops_file: User uploads

    Returns:
        Same structure as generate_w3a_for_api() with NM-specific data.
    """
    from apps.public_core.services.nm_well_import import import_nm_well
    from apps.public_core.services.nm_extraction_mapper import (
        map_nm_well_to_extractions,
        create_nm_extracted_document_data,
        _county_from_api,
        _extract_county,
        _extract_township_range,
    )

    logger.info("=" * 80)
    logger.info("🆕 NM W-3A ORCHESTRATOR - Starting generation for NM API: %s", api_number)
    logger.info("=" * 80)

    warnings = []
    errors = []
    created: List[Dict[str, Any]] = []
    snapshot_id = None
    well_enriched = False

    try:
        # ============================================================
        # STEP 1: AUTO-IMPORT NM WELL
        # ============================================================
        logger.info("\n🏛️ STEP 1: Auto-importing NM well...")

        try:
            import_result = import_nm_well(api_number, update_existing=confirm_fact_updates)
            well = import_result.get("well")
            scraped_data = import_result.get("scraped_data", {})
            import_status = import_result.get("status")

            logger.info(f"   ✅ Well import: {import_status}")
            logger.info(f"   📍 Well: {scraped_data.get('well_name', 'Unknown')}")
            logger.info(f"   📍 Operator: {scraped_data.get('operator_name', 'Unknown')}")

            if import_result.get("errors"):
                warnings.extend(import_result["errors"])

            well_enriched = True

        except Exception as e:
            logger.error(f"   ❌ Well import failed: {e}")
            errors.append(f"Failed to import NM well: {e}")
            # Create a minimal well entry
            api14 = api_number + "0000" if len(api_number) == 10 else api_number
            well, _ = WellRegistry.objects.get_or_create(
                api14=api14,
                defaults={"state": "NM"}
            )
            scraped_data = {}

        # ============================================================
        # STEP 2: FETCH NM EXTRACTION DATA
        # ============================================================
        logger.info("\n📄 STEP 2: Fetching NM well data via scraper...")

        nm_data = {}
        if input_mode in ("extractions", "hybrid"):
            try:
                nm_data = fetch_nm_extraction_data(api_number)
                logger.info(f"   ✅ Scraped data retrieved")
                logger.info(f"   📄 Documents found: {len(nm_data.get('documents', []))}")
                logger.info(f"   📄 Combined PDF URL: {nm_data.get('combined_pdf_url', 'N/A')}")
            except Exception as e:
                logger.warning(f"   ⚠️ Scraping failed: {e}")
                warnings.append(f"NM OCD scraping failed: {e}")

        # ============================================================
        # STEP 3: CREATE PSEUDO-EXTRACTION
        # ============================================================
        logger.info("\n📋 STEP 3: Creating extraction record...")

        extraction = None
        if nm_data.get("extraction"):
            extraction_data = create_nm_extracted_document_data(
                well_data=nm_data.get("well_data", {}),
                documents=nm_data.get("documents", []),
                combined_pdf_url=nm_data.get("combined_pdf_url"),
            )

            with transaction.atomic():
                ed = ExtractedDocument.objects.create(
                    well=well,
                    api_number=api_number,
                    document_type=extraction_data["document_type"],
                    source_path=extraction_data["source_path"],
                    model_tag=extraction_data["model_tag"],
                    status=extraction_data["status"],
                    errors=extraction_data["errors"],
                    json_data=extraction_data["json_data"],
                )
                extraction = ed
                created.append({
                    "document_type": "c105",
                    "extracted_document_id": str(ed.id),
                    "source": "nm_ocd_scraper",
                })
                logger.info(f"   ✅ Created ExtractedDocument: {ed.id}")

        # ============================================================
        # STEP 4: PROCESS USER UPLOADS (if any)
        # ============================================================
        if input_mode in ("user_files", "hybrid"):
            logger.info("\n📤 STEP 4: Processing user file uploads...")
            # TODO: Process user-uploaded files for NM wells
            # This follows the same pattern as TX but stores as NM document types
            pass

        # ============================================================
        # STEP 5: BUILD NM PLAN
        # ============================================================
        logger.info("\n🏗️ STEP 5: Building NM plan...")

        from apps.kernel.handlers.nm.handler import NMJurisdictionHandler

        nm_handler = NMJurisdictionHandler()
        nm_extractions = []

        # Build extraction list from the ExtractedDocument created in Step 3
        if extraction is not None:
            nm_extractions.append({
                "c105": extraction.json_data,
                "document_type": "c105",
            })
        else:
            # Fall back to any existing c105 in the database for this API
            existing_c105 = ExtractedDocument.objects.filter(
                api_number=api_number, document_type="c105"
            ).order_by("-created_at").first()
            if existing_c105:
                nm_extractions.append({
                    "c105": existing_c105.json_data,
                    "document_type": "c105",
                })

        # Derive geometry from extractions
        geometry = nm_handler.derive_geometry(nm_extractions)

        # Build well_info from scraped data
        well_data = nm_data.get("well_data", {})
        _api_str = well_data.get("api10") or well_data.get("api14") or api_number
        _county = (
            _county_from_api(_api_str)
            or _extract_county(well_data.get("surface_location", ""))
        )
        _township, _range = _extract_township_range(well_data.get("surface_location", ""))
        well_info = {
            "api_number": api_number,
            "api14": well_data.get("api14", api_number + "0000"),
            "well_name": well_data.get("well_name", ""),
            "operator": well_data.get("operator_name", ""),
            "county": _county,
            "township": _township,
            "range": _range,
            "field": well_data.get("formation", ""),
            "total_depth": well_data.get("tvd_ft"),
        }

        # Build resolved facts
        facts = nm_handler.build_resolved_facts(well_info, geometry, nm_extractions)

        # Check if we have enough data to generate a plan
        has_casing = bool(geometry.get("casing_strings"))

        if has_casing:
            logger.info("   ✅ Casing data found — generating NM plan from facts")
            # Load policy and generate plan
            policy = nm_handler.load_effective_policy(facts, geometry)
            policy["complete"] = True
            policy["jurisdiction"] = "NM"
            policy["form"] = "C-103"

            plan_result = plan_from_facts(facts, policy)

            plan_output = {
                "jurisdiction": "NM",
                "api": api_number,
                "api10": well_data.get("api10", api_number),
                "api14": well_data.get("api14", api_number + "0000"),
                "well_data": well_data,
                "extraction": nm_data.get("extraction", {}),
                "documents": nm_data.get("documents", []),
                "combined_pdf_url": nm_data.get("combined_pdf_url"),
                "geometry": geometry,
                "facts": facts,
                **plan_result,  # includes steps, citations, constraints, kernel_version
                "plan_notes": [
                    "NM plugging plan generated from C-105 extraction data.",
                    f"Region: {facts.get('county', 'unknown')} county.",
                ],
            }
            logger.info(f"   ✅ NM plan built: {len(plan_output.get('steps', []))} steps")
        else:
            logger.info("   ⚠️ No casing data — returning placeholder requiring manual entry")
            missing_data = []
            if not geometry.get("casing_strings"):
                missing_data.append("casing_record")
            if not geometry.get("perforations"):
                missing_data.append("producing_injection_disposal_interval")
            if not geometry.get("formation_tops"):
                missing_data.append("formation_record")

            plan_output = {
                "jurisdiction": "NM",
                "api": api_number,
                "api10": well_data.get("api10", api_number),
                "api14": well_data.get("api14", api_number + "0000"),
                "well_data": well_data,
                "extraction": nm_data.get("extraction", {}),
                "documents": nm_data.get("documents", []),
                "combined_pdf_url": nm_data.get("combined_pdf_url"),
                "geometry": geometry,
                "facts": facts,
                "steps": [],
                "plan_notes": [
                    "NM well data scraped from OCD portal.",
                    "Casing record must be entered manually from well file documents.",
                    "Plan will be generated after casing data is confirmed.",
                ],
                "requires_manual_entry": True,
                "missing_data": missing_data,
            }

        # ============================================================
        # STEP 6: CREATE PLANSNAPSHOT
        # ============================================================
        logger.info("\n💾 STEP 6: Creating PlanSnapshot...")

        try:
            plan_id = f"{api_number}:nm:{plugs_mode}"
            variant_label = plugs_mode

            snapshot = PlanSnapshot.objects.create(
                well=well,
                plan_id=plan_id,
                kind=PlanSnapshot.KIND_BASELINE,
                payload=plan_output,
                kernel_version="",
                policy_id="nm.plugging",
                overlay_id="",
                extraction_meta={
                    "source": "nm_ocd_scraper",
                    "documents_count": len(nm_data.get("documents", [])),
                    "combined_pdf_url": nm_data.get("combined_pdf_url"),
                },
                visibility=PlanSnapshot.VISIBILITY_PUBLIC,
                tenant_id=_tenant_uuid_for_write(request),
                workspace=workspace,
                status=PlanSnapshot.STATUS_DRAFT,
            )
            snapshot_id = str(snapshot.id)
            logger.info(f"   ✅ PlanSnapshot created: {snapshot_id}")

        except Exception as e:
            logger.exception("Failed to persist PlanSnapshot")
            warnings.append(f"Snapshot persistence failed: {e}")

        # ============================================================
        # STEP 7: RESPONSE
        # ============================================================
        logger.info("\n📦 STEP 7: Formatting response...")

        return {
            "success": True,
            "jurisdiction": "NM",
            "w3a_data": plan_output,
            "snapshot_id": snapshot_id,
            "auto_generated": False,  # NM requires manual data entry
            "extraction_count": len(created),
            "well_enriched": well_enriched,
            "nm_data": {
                "well_data": nm_data.get("well_data", {}),
                "documents": nm_data.get("documents", []),
                "combined_pdf_url": nm_data.get("combined_pdf_url"),
            },
            "validation": {
                "warnings": warnings,
                "errors": errors,
            },
        }

    except Exception as e:
        logger.error(f"❌ NM W-3A orchestration failed: {e}", exc_info=True)
        return {
            "success": False,
            "jurisdiction": "NM",
            "error": str(e),
            "w3a_data": None,
            "snapshot_id": None,
            "auto_generated": False,
            "extraction_count": len(created),
            "well_enriched": well_enriched,
            "validation": {
                "warnings": warnings,
                "errors": [str(e)] + errors,
            },
        }

