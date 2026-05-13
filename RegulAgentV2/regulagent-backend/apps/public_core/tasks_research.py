"""
Celery tasks for Research Session document indexing.

Uses a group + self-finalization pattern: each index task atomically increments
a counter and triggers finalize when all docs are done. A watchdog timer
provides a safety net for SIGKILL'd tasks whose counter never incremented.
"""
import logging
import re

from celery import group, shared_task

from apps.public_core.models import ResearchSession, WellRegistry
from apps.tenants.context import set_current_tenant
from apps.public_core.services.adapters.base import DocumentSpec
from apps.public_core.services.document_pipeline import (
    get_adapter,
    index_single_document,
)

logger = logging.getLogger(__name__)


def _increment_and_maybe_finalize(session_id: str) -> None:
    """Atomically increment indexed_documents and trigger finalize if all docs are done.

    The increment is capped at total_documents — extra calls (e.g. from edge-case
    error paths) are silently ignored so the counter never exceeds the expected total.
    """
    from django.db.models import F
    from django.db.models.functions import Least
    # Atomically clamp the counter at total_documents in the DB — safe under concurrency.
    # Two workers passing the old __lt filter simultaneously could both increment and
    # push indexed_documents above total_documents. Least() prevents that race entirely.
    ResearchSession.objects.filter(id=session_id).update(
        indexed_documents=Least(F("indexed_documents") + 1, F("total_documents"))
    )
    # Re-read to check completion
    session = ResearchSession.objects.get(id=session_id)
    if session.indexed_documents >= session.total_documents and session.total_documents > 0:
        logger.info(
            f"[Research] All {session.total_documents} docs indexed for session {session_id}, "
            f"triggering finalize"
        )
        finalize_session_task.delay([], session_id=str(session_id))


def _split_pdf_into_chunks(pdf_path, page_count, max_pages, neubus_doc=None, lease=None):
    """
    Split a large PDF into smaller chunk files and create NeubusDocument records for each.

    Returns list of NeubusDocument instances (one per chunk).
    """
    import fitz
    import math
    from pathlib import Path
    from apps.public_core.models.neubus_lease import NeubusDocument

    num_chunks = math.ceil(page_count / max_pages)
    chunk_docs = []
    parent_path = Path(pdf_path)

    for i in range(num_chunks):
        start_page = i * max_pages
        end_page = min((i + 1) * max_pages, page_count)  # exclusive

        # Create chunk PDF file
        chunk_filename = f"{parent_path.stem}_pt{i+1}{parent_path.suffix}"
        chunk_path = parent_path.parent / chunk_filename

        src_pdf = fitz.open(str(pdf_path))
        src_pdf.select(list(range(start_page, end_page)))
        src_pdf.save(str(chunk_path))
        src_pdf.close()

        # Create NeubusDocument for this chunk
        chunk_neubus_filename = f"{parent_path.stem}_pt{i+1}{parent_path.suffix}"

        chunk_nd, _ = NeubusDocument.objects.get_or_create(
            neubus_filename=chunk_neubus_filename,
            defaults={
                "lease": lease or neubus_doc.lease,
                "well_number": neubus_doc.well_number if neubus_doc else "",
                "api": neubus_doc.api if neubus_doc else "",
                "pages": end_page - start_page,
                "local_path": str(chunk_path),
                "parent_document": neubus_doc,
                "part_number": i + 1,
                "part_total": num_chunks,
                "classification_status": "pending",
                "extraction_status": "pending",
            },
        )
        chunk_docs.append(chunk_nd)
        logger.info(
            f"[split] Created chunk {i+1}/{num_chunks}: {chunk_neubus_filename} "
            f"(pages {start_page}-{end_page-1}, {end_page - start_page} pages)"
        )

    return chunk_docs


@shared_task
def _on_index_task_error(task_id, session_id: str):
    """Error callback: increment counter even when index_document_task is SIGKILL'd."""
    logger.warning(
        f"[Research] index_document_task {task_id} failed hard for session {session_id}"
    )
    _increment_and_maybe_finalize(session_id)


@shared_task(bind=True, max_retries=2, time_limit=600, soft_time_limit=540)
def start_research_session_task(self, session_id: str):
    """
    Entry point: fetch document list, then dispatch parallel index tasks via group.

    Called when a new research session is created via the API.
    """
    try:
        session = ResearchSession.objects.get(id=session_id)
    except ResearchSession.DoesNotExist:
        logger.error(f"ResearchSession {session_id} not found")
        return {"error": f"Session {session_id} not found"}

    if session.tenant_id:
        from apps.tenants.models import Tenant
        tenant = Tenant.objects.get(id=session.tenant_id)
        set_current_tenant(tenant)

    # Update status
    session.status = "fetching"
    session.celery_task_id = self.request.id or ""
    session.save(update_fields=["status", "celery_task_id"])

    try:
        # Resolve well
        from apps.public_core.services.api_normalization import normalize_api_14digit
        api14_correct = normalize_api_14digit(session.api_number) or re.sub(r"\D+", "", str(session.api_number or ""))
        well = WellRegistry.objects.filter(api14=api14_correct).first()
        if not well:
            # Create a minimal WellRegistry so documents can be linked
            well, created = WellRegistry.objects.get_or_create(
                api14=api14_correct,
                defaults={"state": session.state or ""},
            )
            if created:
                logger.info(f"Created WellRegistry api14={well.api14} for research session {session_id}")
            else:
                logger.info(f"Reusing WellRegistry api14={well.api14} for research session {session_id}")
        session.well = well
        session.save(update_fields=["well"])

        # Build lease-well map for cross-reference during extraction
        lease_well_map = {}
        lease_id = ""
        if session.well:
            lease_id = session.well.lease_id or ""
            if lease_id:
                try:
                    from apps.public_core.services.well_registry_enrichment import build_lease_well_map
                    lease_well_map = build_lease_well_map(lease_id, session.state)
                except Exception as e:
                    logger.warning(f"Failed to build lease-well map: {e}")

            # Store on session metadata for reference
            session.metadata = session.metadata or {}
            session.metadata["lease_well_map"] = lease_well_map
            session.metadata["lease_id"] = lease_id
            session.save(update_fields=["metadata"])

        # Track tenant-well engagement
        if session.tenant_id:
            try:
                from apps.tenant_overlay.services.engagement_tracker import track_well_interaction
                track_well_interaction(
                    tenant_id=session.tenant_id,
                    well=well,
                    interaction_type="researched",
                )
            except Exception as e:
                logger.warning(f"Failed to track well interaction for session {session_id}: {e}")

        # Fetch document list
        adapter = get_adapter(session.state)
        if session.force_fetch and session.state == "TX":
            # Force re-download from Neubus, bypassing cache
            from apps.public_core.services.neubus_ingest import ingest_lease
            from apps.public_core.models.neubus_lease import NeubusDocument
            logger.info(f"[Research] Force fetch: re-downloading from Neubus for {session.api_number}")
            lease = ingest_lease(session.api_number)
            # Reset all document statuses so they get re-processed
            reset_count = NeubusDocument.objects.filter(lease=lease).update(
                classification_status="pending",
                extraction_status="pending",
            )
            logger.info(
                f"[Research] Force fetch: reset {reset_count} NeubusDocument statuses "
                f"for lease {lease.lease_id}"
            )
        doc_list = adapter.fetch_document_list(session.api_number)

        if not doc_list:
            # Surface diagnostic info from the adapter
            fetch_error = getattr(adapter, '_last_fetch_error', None)
            if fetch_error:
                error_msg = fetch_error.get("message", "No documents found for this well.")
                logger.warning(
                    f"[Research] Scraper diagnostics for session {session_id}: "
                    f"status={fetch_error.get('scraper_status')}, "
                    f"api_search={fetch_error.get('api_search')}"
                )
            else:
                error_msg = "No documents found for this well."
            session.status = "error"
            session.error_message = error_msg
            session.save(update_fields=["status", "error_message"])
            return {"error": error_msg}

        # Cache doc metadata
        session.total_documents = len(doc_list)
        session.document_list = [
            {
                "filename": d.filename,
                "url": d.url,
                "file_size": d.file_size,
                "date": d.date,
                "doc_type": d.doc_type,
            }
            for d in doc_list
        ]
        session.status = "indexing"
        session.save(update_fields=["total_documents", "document_list", "status"])

        # Mark well as actively indexing
        if session.well:
            session.well.data_status = "indexing"
            session.well.save(update_fields=["data_status"])

        def _make_doc_spec(d):
            spec = {
                "filename": d.filename,
                "url": d.url,
                "local_path": d.local_path,
                "file_size": d.file_size,
                "date": d.date,
                "doc_type": d.doc_type,
                "metadata": d.metadata,
            }
            if session.force_fetch:
                spec["metadata"] = dict(spec["metadata"] or {})
                spec["metadata"]["force_fetch"] = True
            return spec

        # Dispatch all docs as a flat group (no chord — resilient to individual task SIGKILL)
        error_callback = _on_index_task_error.s(session_id=str(session.id))
        all_tasks = group(
            index_document_task.s(
                session_id=str(session.id),
                doc_spec=_make_doc_spec(d),
                lease_id=lease_id,
                lease_well_map=lease_well_map,
                state=session.state or "",
            ).on_error(error_callback)
            for d in doc_list
        )
        all_tasks.apply_async()

        # Watchdog: tasks run in parallel, so timeout = single task time_limit + buffer
        # time_limit=300 on index_document_task, but most finish in <60s
        watchdog_delay = min(600, max(120, len(doc_list) * 10))
        finalize_session_task.apply_async(
            args=[[]],
            kwargs={"session_id": str(session.id)},
            countdown=watchdog_delay,
        )

        return {"status": "indexing", "total_documents": len(doc_list)}

    except Exception as e:
        logger.exception(f"Failed to start research session {session_id}: {e}")
        session.status = "error"
        session.error_message = str(e)
        session.save(update_fields=["status", "error_message"])
        # Revert data_status on error
        if session.well:
            session.well.data_status = "cold_storage"
            session.well.save(update_fields=["data_status"])
        raise self.retry(exc=e, countdown=30)


@shared_task(bind=True, max_retries=2, acks_late=True, time_limit=300, soft_time_limit=270)
def index_document_task(
    self,
    session_id: str,
    doc_spec: dict,
    lease_id: str = "",
    lease_well_map: dict | None = None,
    state: str = "",
):
    """
    Per-document worker: download → classify → extract → vectorize.

    Increments session counter on completion and triggers finalize when all docs are done.
    """
    try:
        session = ResearchSession.objects.get(id=session_id)
    except ResearchSession.DoesNotExist:
        return {"success": False, "error": "Session not found"}

    if session.tenant_id:
        from apps.tenants.models import Tenant
        tenant = Tenant.objects.get(id=session.tenant_id)
        set_current_tenant(tenant)

    doc = DocumentSpec(
        filename=doc_spec["filename"],
        url=doc_spec.get("url"),
        local_path=doc_spec.get("local_path"),
        file_size=doc_spec.get("file_size"),
        date=doc_spec.get("date"),
        doc_type=doc_spec.get("doc_type"),
        metadata=doc_spec.get("metadata"),
    )

    well = session.well

    # Resolve lease context — fall back to session metadata if not passed directly
    if not lease_well_map and hasattr(session, "metadata") and session.metadata:
        lease_well_map = session.metadata.get("lease_well_map") or {}
    if not lease_id and hasattr(session, "metadata") and session.metadata:
        lease_id = session.metadata.get("lease_id") or ""
    if not state:
        state = session.state or ""
    lease_well_map = lease_well_map or {}

    try:
        logger.info(
            f"[index_document_task] {doc.filename}: metadata={doc.metadata}, "
            f"session.state={session.state}"
        )
        # Route TX Neubus documents through Vision pipeline for per-form extraction
        # Check metadata flag OR if a NeubusDocument record exists for this filename
        is_rrc = bool(doc.metadata and doc.metadata.get("rrc_source"))
        from apps.public_core.models.neubus_lease import NeubusDocument as _ND
        is_neubus = (
            not is_rrc
            and (
                (doc.metadata and doc.metadata.get("neubus_lease_id"))
                or _ND.objects.filter(neubus_filename=doc.filename).exists()
            )
        )
        logger.info(f"[index_document_task] {doc.filename}: is_rrc={is_rrc}, is_neubus={is_neubus}, routing={'rrc' if is_rrc else 'vision' if is_neubus and session.state == 'TX' else 'generic'}")
        if is_rrc:
            # RRC path: clean PDFs, use generic pipeline (no Vision needed)
            ed = index_single_document(doc, session.api_number, well, session)
            _increment_and_maybe_finalize(str(session.id))
            if ed:
                return {"success": True, "ed_id": str(ed.id), "doc_type": ed.document_type, "source": "rrc"}
            else:
                return {"success": False, "reason": "skipped_or_unknown_type", "filename": doc.filename}
        elif is_neubus and session.state == "TX":
            from pathlib import Path
            from apps.public_core.models.neubus_lease import NeubusDocument
            from apps.public_core.services.neubus_classifier import (
                classify_document_pages_v2,
            )
            from apps.public_core.services.neubus_extractor import extract_form_groups
            from apps.public_core.services.document_pipeline import get_adapter
            from apps.public_core.services.document_segmenter import persist_segments

            # Download the document
            adapter = get_adapter(session.state)
            local_path = adapter.download_document(doc)

            # Look up NeubusDocument for status tracking
            neubus_doc = NeubusDocument.objects.filter(
                neubus_filename=doc.filename
            ).first()

            # Idempotency: skip if already fully processed (unless force_fetch)
            force_fetch = doc.metadata and doc.metadata.get("force_fetch", False)
            if not force_fetch and neubus_doc and neubus_doc.classification_status == "complete" and neubus_doc.extraction_status == "complete":
                from apps.public_core.models import ExtractedDocument
                existing_eds = ExtractedDocument.objects.filter(neubus_filename=doc.filename).count()
                if existing_eds > 0:
                    logger.info(f"Skipping already-processed TX Neubus doc {doc.filename} ({existing_eds} EDs exist)")
                    _increment_and_maybe_finalize(str(session.id))
                    return {"success": True, "skipped": True, "existing_eds": existing_eds, "filename": doc.filename}
            if force_fetch:
                logger.info(f"[index_document_task] force_fetch=True for {doc.filename}, bypassing idempotency check")

            # Pre-flight: check page count and split large PDFs into chunks
            MAX_PAGES_PER_CHUNK = 30
            try:
                import fitz
                _pdf = fitz.open(str(local_path))
                _page_count = _pdf.page_count
                _pdf.close()

                if _page_count > MAX_PAGES_PER_CHUNK and (not neubus_doc or not neubus_doc.parent_document):
                    # Split into physical chunk PDFs and dispatch each as its own task
                    logger.info(
                        f"[index_document_task] Splitting {doc.filename} "
                        f"({_page_count} pages) into chunks of {MAX_PAGES_PER_CHUNK}"
                    )
                    chunk_docs = _split_pdf_into_chunks(
                        local_path, _page_count, MAX_PAGES_PER_CHUNK,
                        neubus_doc=neubus_doc, lease=neubus_doc.lease if neubus_doc else None,
                    )
                    if chunk_docs:
                        # Mark parent as "chunked" so it doesn't get re-processed
                        if neubus_doc:
                            neubus_doc.classification_status = "chunked"
                            neubus_doc.save(update_fields=["classification_status"])

                        # Bump total_documents to account for new chunks (minus parent)
                        extra = len(chunk_docs) - 1  # parent already counted
                        if extra > 0:
                            session.total_documents = (session.total_documents or 0) + extra
                            session.save(update_fields=["total_documents"])

                        # Dispatch chunk tasks with staggered delays
                        for i, chunk_nd in enumerate(chunk_docs):
                            chunk_doc_spec = {
                                "filename": chunk_nd.neubus_filename,
                                "local_path": chunk_nd.local_path,
                                "metadata": {
                                    "neubus_lease_id": doc.metadata.get("neubus_lease_id") if doc.metadata else None,
                                    "chunk_of": doc.filename,
                                },
                            }
                            index_document_task.apply_async(
                                args=[str(session.id), chunk_doc_spec],
                                kwargs={
                                    "lease_id": lease_id,
                                    "lease_well_map": lease_well_map,
                                    "state": state,
                                },
                                countdown=i * 30,  # stagger to avoid OOM
                            )

                        # Do NOT increment here — each chunk task increments its own count.
                        # The parent slot was replaced by len(chunk_docs) slots when
                        # total_documents was adjusted above, so only the chunks should call
                        # _increment_and_maybe_finalize().
                        return {
                            "success": True,
                            "chunked": True,
                            "chunks": len(chunk_docs),
                            "filename": doc.filename,
                            "pages": _page_count,
                        }
            except Exception as split_err:
                logger.warning(f"[index_document_task] Pre-flight/split failed for {doc.filename}: {split_err}")

            # Classify pages → group forms → extract (text-first with Vision fallback)
            form_groups = classify_document_pages_v2(
                local_path, state="TX", neubus_doc=neubus_doc
            )

            if not form_groups:
                logger.warning(f"No form groups found in TX Neubus doc {doc.filename}")
                _increment_and_maybe_finalize(str(session.id))
                return {"success": False, "reason": "no_forms", "filename": doc.filename}

            # Extract and persist (creates EDs with neubus_filename set)
            results = extract_form_groups(
                pdf_path=local_path,
                form_groups=form_groups,
                neubus_doc=neubus_doc,
                well=well,
                api_number=session.api_number,
                neubus_filename=doc.filename,
                file_hash=neubus_doc.file_hash if neubus_doc else "",
                lease_id=lease_id,
                lease_well_map=lease_well_map,
                state=state,
                neubus_well_number=neubus_doc.well_number if neubus_doc else "",
            )

            # Update session progress
            _increment_and_maybe_finalize(str(session.id))

            succeeded = sum(1 for r in results if r.status == "success")
            return {
                "success": succeeded > 0,
                "forms_extracted": len(results),
                "filename": doc.filename,
            }
        else:
            # Default path: generic pipeline (NM or non-Neubus docs)
            ed = index_single_document(doc, session.api_number, well, session)
            _increment_and_maybe_finalize(str(session.id))
            if ed:
                return {"success": True, "ed_id": str(ed.id), "doc_type": ed.document_type}
            else:
                return {"success": False, "reason": "skipped_or_unknown_type", "filename": doc.filename}
    except Exception as e:
        logger.exception(f"index_document_task failed for {doc.filename}: {e}")
        _increment_and_maybe_finalize(str(session.id))
        return {"success": False, "error": str(e), "filename": doc.filename}


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def enrich_well_structured_task(self, api14: str):
    """Enrich WellRegistry from structured RRC web scrapers (wellbore, lease detail)."""
    logger.info(f"[StructuredScrape] Starting for api14={api14}")
    try:
        from apps.public_core.services.well_registry_enrichment import enrich_from_structured_scrapers
        from apps.public_core.models import WellRegistry
        well = WellRegistry.objects.filter(api14=api14).first()
        if not well:
            logger.warning(f"[StructuredScrape] No WellRegistry for api14={api14}")
            return {"status": "skipped", "reason": "no_well"}
        updated = enrich_from_structured_scrapers(well)
        logger.info(f"[StructuredScrape] Done for api14={api14}, updated={updated}")
        return {"status": "success", "updated": updated}
    except Exception as exc:
        logger.exception(f"[StructuredScrape] Failed for api14={api14}: {exc}")
        raise self.retry(exc=exc)


@shared_task
def finalize_session_task(results: list, session_id: str):
    """
    Finalize callback: runs when all index tasks complete (triggered by the last
    task via _increment_and_maybe_finalize) or by the watchdog timer.

    Idempotent: if session is already "ready", logs and returns.
    """
    try:
        session = ResearchSession.objects.get(id=session_id)
    except ResearchSession.DoesNotExist:
        logger.error(f"ResearchSession {session_id} not found in finalize")
        return

    from django.db import transaction

    # Count results from this batch (may be empty list from watchdog or self-finalize)
    results = results or []
    succeeded = sum(1 for r in results if r.get("success"))
    failed = sum(1 for r in results if not r.get("success"))

    # Atomically check and update status to prevent dual-finalize race
    with transaction.atomic():
        session = ResearchSession.objects.select_for_update().get(id=session_id)
        was_already_ready = session.status == "ready"

        if was_already_ready:
            logger.info(
                f"[Research] Finalize called for session {session_id} but already ready, skipping"
            )
            return

        # Watchdog: if still indexing, finalize with partial results
        if session.status == "indexing":
            if session.indexed_documents < session.total_documents:
                logger.warning(
                    f"[Research] Watchdog finalize for session {session_id}: "
                    f"{session.indexed_documents}/{session.total_documents} indexed, "
                    f"forcing ready status"
                )
            session.status = "ready"
            session.error_message = ""
            session.save(update_fields=["status", "error_message"])
            should_enrich = session.well_id is not None
        elif succeeded > 0:
            session.status = "ready"
            session.error_message = ""
            session.save(update_fields=["status", "error_message"])
            should_enrich = session.well_id is not None
        else:
            session.status = "error"
            errors = [r.get("error", r.get("reason", "unknown")) for r in results if not r.get("success")]
            session.error_message = (
                f"All {failed} documents failed to index. Errors: {'; '.join(errors[:5])}"
            )
            session.save(update_fields=["status", "error_message"])
            should_enrich = False

    # Update well data_status based on session outcome
    if session.well:
        if session.status == "ready":
            session.well.data_status = "ready"
            session.well.save(update_fields=["data_status"])
        elif session.status == "error":
            session.well.data_status = "cold_storage"
            session.well.save(update_fields=["data_status"])

    logger.info(
        f"Research session {session_id} finalized: "
        f"{session.indexed_documents}/{session.total_documents} indexed, status={session.status}"
    )

    # Run enrichment OUTSIDE the lock
    if should_enrich:
        # Step 1: Enrich WellRegistry fields
        try:
            from apps.public_core.services.well_registry_enrichment import enrich_well_registry_from_documents
            from apps.public_core.models.extracted_document import ExtractedDocument
            extracted_docs = list(ExtractedDocument.objects.filter(
                api_number=session.api_number, status="success"))
            if extracted_docs:
                enrich_well_registry_from_documents(session.well, extracted_docs)
                logger.info(f"Enriched WellRegistry for api14={session.well.api14}")
        except Exception as e:
            logger.warning(f"Failed to enrich WellRegistry for session {session_id}: {e}")

        # Step 2: Trigger component extraction
        if session.tenant_id:
            try:
                from apps.public_core.tasks import extract_and_populate_components
                extract_and_populate_components.apply_async(
                    args=[session.well.api14, session.tenant_id],
                    countdown=45,
                )
                logger.info(f"Triggered extract_and_populate_components for api14={session.well.api14} (45s delay)")
            except Exception as e:
                logger.warning(f"Failed to trigger component extraction for session {session_id}: {e}")

        # Step 3: Trigger structured scraping
        try:
            enrich_well_structured_task.delay(session.well.api14)
            logger.info(f"Triggered enrich_well_structured_task for api14={session.well.api14}")
        except Exception as e:
            logger.warning(f"Failed to trigger structured scraping for session {session_id}: {e}")

        # Step 4: Build/refresh well timeline
        try:
            from apps.public_core.services.timeline_builder import refresh_timeline
            refresh_timeline(session.well)
            logger.info(f"Refreshed timeline for api14={session.well.api14}")
        except Exception as e:
            logger.warning(f"Failed to refresh timeline for session {session_id}: {e}")


@shared_task(bind=True, time_limit=300, soft_time_limit=280)
def classify_extract_document_task(self, neubus_doc_id: int, api_number: str):
    """
    Classify + extract a single NeubusDocument PDF.

    Designed to run across prefork workers in parallel via chord/group.
    Each task handles one PDF: classify pages → group forms → extract.
    """
    from pathlib import Path
    from apps.public_core.models.neubus_lease import NeubusDocument
    from apps.public_core.services.neubus_classifier import (
        classify_document_pages_v2,
        FormGroup,
    )
    from apps.public_core.services.neubus_extractor import extract_form_groups

    try:
        doc = NeubusDocument.objects.get(id=neubus_doc_id)
    except NeubusDocument.DoesNotExist:
        logger.error(f"NeubusDocument {neubus_doc_id} not found")
        return {"success": False, "error": f"Document {neubus_doc_id} not found"}

    pdf_path = Path(doc.local_path)
    if not pdf_path.exists():
        logger.warning(f"PDF not found at {doc.local_path}")
        return {"success": False, "error": f"PDF not found: {doc.local_path}"}

    # Skip already-processed documents
    if doc.classification_status == "complete" and doc.extraction_status == "complete":
        logger.info(f"Skipping already-processed {doc.neubus_filename}")
        return {"success": True, "skipped": True, "filename": doc.neubus_filename}

    try:
        # Classify pages
        if doc.classification_status != "complete":
            form_groups = classify_document_pages_v2(
                pdf_path, state="TX", neubus_doc=doc
            )
        else:
            # Reconstruct form groups from stored classification
            from apps.public_core.services.neubus_classifier import FormGroup
            form_groups = []
            for form_type, pages in doc.form_types_by_page.items():
                form_groups.append(FormGroup(
                    form_type=form_type,
                    pages=[p - 1 for p in pages],  # Convert to 0-indexed
                ))

        # Extract form data
        if doc.extraction_status != "complete":
            # Resolve lease context for attribution
            _lease_id = ""
            _lease_well_map = {}
            _state = "TX"
            if doc.lease and doc.lease.lease_id:
                _lease_id = doc.lease.lease_id
            if _lease_id:
                try:
                    from apps.public_core.services.well_registry_enrichment import build_lease_well_map
                    _lease_well_map = build_lease_well_map(_lease_id, _state)
                except Exception:
                    pass

            extract_form_groups(
                pdf_path=pdf_path,
                form_groups=form_groups,
                neubus_doc=doc,
                api_number=api_number,
                neubus_filename=doc.neubus_filename,
                file_hash=doc.file_hash,
                lease_id=_lease_id,
                lease_well_map=_lease_well_map,
                state=_state,
                neubus_well_number=doc.well_number or "",
            )

        return {
            "success": True,
            "filename": doc.neubus_filename,
            "forms": len(form_groups),
        }

    except Exception as e:
        logger.exception(f"classify_extract_document_task failed for doc {neubus_doc_id}: {e}")
        return {"success": False, "error": str(e), "filename": doc.neubus_filename}
