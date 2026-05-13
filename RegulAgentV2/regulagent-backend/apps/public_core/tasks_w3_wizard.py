"""
Celery tasks for the W-3 Daily Ticket Upload & Reconciliation Wizard.

Task lifecycle:
    parse_wizard_tickets       → STATUS_PARSING → STATUS_PARSED
    run_wizard_reconciliation  → STATUS_RECONCILED
    generate_wizard_w3         → STATUS_GENERATING → STATUS_COMPLETED
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from dataclasses import asdict
from datetime import date, time as dt_time
from typing import Any, Dict, List, Optional

from celery import shared_task
from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import default_storage
from django.utils import timezone

from apps.tenants.context import set_current_tenant
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


def _jsonable(obj):
    """Recursively convert date/time objects to ISO strings for JSON serialization."""
    import datetime
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, dt_time):
        return obj.isoformat()
    return obj


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _resolve_storage_path(storage_key: str) -> str:
    """Download a file from Django's storage backend to a local temp path.

    Returns the local file path. Caller is responsible for cleanup.
    When using local storage, just returns the direct path.
    """
    if hasattr(default_storage, 'path'):
        # Local storage — return direct path
        try:
            return default_storage.path(storage_key)
        except (NotImplementedError, SuspiciousFileOperation):
            pass

    # S3 or other remote storage — download to temp file
    ext = os.path.splitext(storage_key)[1]
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        with default_storage.open(storage_key, 'rb') as src:
            for chunk in src.chunks(8192) if hasattr(src, 'chunks') else iter(lambda: src.read(8192), b''):
                tmp.write(chunk)
        tmp.close()
        return tmp.name
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise


# ---------------------------------------------------------------------------
# Task 1 — Parse uploaded daily tickets
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=2)
def parse_wizard_tickets(self, session_id: str) -> None:
    """
    Parse all uploaded daily-ticket documents attached to a W3WizardSession.

    Steps:
    1. Load session
    2. Mark status → STATUS_PARSING
    3. Resolve file paths from uploaded_documents storage_keys
    4. Call UniversalTicketParser.parse_files()
    5. Persist parse_result; advance to STATUS_PARSED / step 2
    """
    from apps.public_core.models.w3_wizard_session import W3WizardSession
    from apps.public_core.services.universal_ticket_parser import UniversalTicketParser

    try:
        session = W3WizardSession.objects.get(pk=session_id)
    except W3WizardSession.DoesNotExist:
        logger.error("parse_wizard_tickets: session %s not found", session_id)
        return

    tenant = Tenant.objects.get(id=session.tenant_id)
    set_current_tenant(tenant)

    # Transition → parsing
    session.status = W3WizardSession.STATUS_PARSING
    session.save(update_fields=["status", "updated_at"])

    try:
        # Build file paths from storage_keys via Django storage backend
        file_paths: List[str] = []
        temp_files: List[str] = []
        for doc in session.uploaded_documents:
            if doc.get("category") == "plan":
                continue  # Plan processed by import_wizard_plan
            if doc.get("is_excluded"):
                continue  # Skip excluded documents
            storage_key = doc.get("storage_key", "")
            if storage_key:
                local_path = _resolve_storage_path(storage_key)
                file_paths.append(local_path)
                # Track temp files for cleanup (only if downloaded from remote storage)
                if not hasattr(default_storage, 'path'):
                    temp_files.append(local_path)

        if not file_paths:
            logger.warning(
                "parse_wizard_tickets: session %s has no uploaded documents to parse",
                session_id,
            )

        # Build well context from plan_snapshot when available
        well_context = None
        if session.plan_snapshot and hasattr(session.plan_snapshot, 'payload') and session.plan_snapshot.payload:
            p = session.plan_snapshot.payload
            well_context = {
                "formations": p.get("formations", []),
                "casing_record": p.get("casing_record", []),
                "well_header": p.get("well_header", {}),
            }

        # Parse all files in one AI call
        result = UniversalTicketParser().parse_files(file_paths, session.api_number, well_context=well_context)

        # Persist result and advance step
        session.parse_result = _jsonable(asdict(result))
        session.status = W3WizardSession.STATUS_PARSED
        session.current_step = 2

        # --- Event compliance flags (non-fatal) ---
        try:
            from apps.public_core.services.event_compliance_checker import check_events
            from apps.kernel.services.jurisdiction_registry import get_handler

            jurisdiction = session.jurisdiction
            handler = get_handler(jurisdiction)
            policy = handler.load_effective_policy(facts={}) if handler else {}
            session.event_compliance_flags = check_events(
                parse_result=session.parse_result,
                policy=policy,
                jurisdiction=jurisdiction,
            )
        except Exception:
            logger.warning(
                "parse_wizard_tickets: event compliance check failed (non-fatal) for %s",
                session_id, exc_info=True,
            )

        session.save(update_fields=["parse_result", "status", "current_step", "event_compliance_flags", "updated_at"])

        # Clean up temp files downloaded from remote storage
        for tmp in temp_files:
            try:
                os.unlink(tmp)
            except OSError:
                pass

        logger.info(
            "parse_wizard_tickets: session %s parsed %d day(s), warnings=%s",
            session_id,
            len(result.days),
            result.warnings,
        )

    except Exception as exc:
        logger.exception(
            "parse_wizard_tickets: error processing session %s: %s", session_id, exc
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30)

        # Final failure — record a soft-error parse_result so UI can surface it
        session.parse_result = {
            "days": [],
            "warnings": [
                f"Parsing failed after {self.max_retries} retries: {exc}"
            ],
            "api_number": session.api_number,
        }
        session.status = W3WizardSession.STATUS_PARSED
        session.current_step = 2
        session.save(update_fields=["parse_result", "status", "current_step", "updated_at"])


# ---------------------------------------------------------------------------
# PDF plan import helper
# ---------------------------------------------------------------------------

def _import_plan_from_pdf(session, file_path: str) -> Dict[str, Any]:
    """Extract a P&A plan from a PDF and create a PlanSnapshot.

    Uses the same AI extraction pipeline as the standard document extractor,
    then normalizes the result into a PlanSnapshot — mirroring what
    import_operator_packet does for .docx files.
    """
    import re
    from pathlib import Path
    from django.db import transaction

    try:
        from apps.public_core.services.openai_extraction import extract_json_from_pdf
        from apps.public_core.services.operator_packet_importer import (
            _normalize_pa_steps_to_plan_format,
        )
        from apps.public_core.models import PlanSnapshot, WellRegistry

        extraction = extract_json_from_pdf(Path(file_path), doc_type="pa_procedure")
        json_data = extraction.json_data or {}

        if extraction.errors and not json_data:
            return {
                "success": False,
                "error": "PDF extraction failed",
                "reasons": extraction.errors,
            }

        # Resolve well — try multiple API number formats to handle varying lengths.
        # The session api_number may be a 10-digit TX format (e.g. "42-461-01623")
        # which strips to 10 digits, while the DB may store 10 or 14-digit api14 values.
        api_digits = re.sub(r"\D+", "", str(session.api_number or ""))
        well = None
        if api_digits:
            # Attempt 1: exact match on the raw digit string (e.g. 10-digit stored as-is)
            well = WellRegistry.objects.filter(api14=api_digits).first()

            # Attempt 2: zero-pad to 14 digits (standard API-14 format)
            if well is None and len(api_digits) <= 14:
                api14 = api_digits.ljust(14, "0")
                well = WellRegistry.objects.filter(api14=api14).first()

            # Attempt 3: use the last 14 digits when the string is longer than 14
            if well is None and len(api_digits) > 14:
                api14 = api_digits[-14:]
                well = WellRegistry.objects.filter(api14=api14).first()

            # Attempt 4: prefix match on the first 8 significant digits (state+county+unique)
            if well is None and len(api_digits) >= 8:
                well = WellRegistry.objects.filter(api14__startswith=api_digits[:8]).first()

        if well is None:
            logger.warning(
                "_import_plan_from_pdf: could not resolve WellRegistry for api_number=%s "
                "(api_digits=%s) — PlanSnapshot will be created without a well FK",
                session.api_number,
                api_digits,
            )

        # Build plan payload
        plan_payload = _normalize_pa_steps_to_plan_format(json_data)

        # Extract wellbore geometry from schematic page via vision
        from apps.public_core.services.well_geometry_builder import (
            extract_geometry_from_plan_pdf,
            normalize_vision_to_well_geometry,
        )
        try:
            vision_geometry = extract_geometry_from_plan_pdf(
                file_path,
                w2_data={"casing_record": plan_payload.get("casing_record", [])}
            )
            if vision_geometry:
                plan_payload["well_geometry"] = normalize_vision_to_well_geometry(vision_geometry)
                logger.info("Stored vision-extracted well_geometry on plan payload")
        except Exception as vision_exc:
            logger.warning(f"Vision WBD extraction failed (non-fatal): {vision_exc}")

        plan_id = f"{session.api_number}:approved"

        with transaction.atomic():
            snapshot = PlanSnapshot.objects.create(
                well=well,
                plan_id=plan_id,
                kind=PlanSnapshot.KIND_APPROVED,
                status=PlanSnapshot.STATUS_AGENCY_APPROVED,
                visibility=PlanSnapshot.VISIBILITY_PRIVATE,
                payload=plan_payload,
                kernel_version="",
                policy_id="",
                overlay_id="",
                extraction_meta={
                    "import_source": "wizard_pdf_upload",
                    "file_name": Path(file_path).name,
                },
                tenant_id=session.tenant_id,
                workspace=session.workspace,
            )

        logger.info(
            "_import_plan_from_pdf: created PlanSnapshot %s for session %s",
            snapshot.id, session.id,
        )
        return {"success": True, "plan_snapshot_id": str(snapshot.id)}

    except Exception as exc:
        logger.exception("_import_plan_from_pdf failed: %s", exc)
        return {
            "success": False,
            "error": "PDF plan import failed",
            "reasons": [str(exc)],
        }


# ---------------------------------------------------------------------------
# Task 1b — Import operator plan from wizard upload
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=2)
def import_wizard_plan(self, session_id: str, storage_key: str) -> None:
    """
    Import an operator P&A packet (.docx or .pdf) uploaded through the W-3 Wizard.

    Steps:
    1. Load session, set status → importing_plan
    2. Build file path from storage_key
    3. Route by extension: .docx → import_packet_headless, .pdf → extract_json_from_pdf
    4. On success: link new PlanSnapshot to session
    5. On failure: record error, retry up to 2 times
    """
    from apps.public_core.models.w3_wizard_session import W3WizardSession

    try:
        session = W3WizardSession.objects.get(pk=session_id)
    except W3WizardSession.DoesNotExist:
        logger.error("import_wizard_plan: session %s not found", session_id)
        return

    tenant = Tenant.objects.get(id=session.tenant_id)
    set_current_tenant(tenant)

    session.status = W3WizardSession.STATUS_IMPORTING_PLAN
    session.save(update_fields=["status", "updated_at"])

    try:
        # Resolve file from Django storage (S3 or local)
        # Retry up to 10 times with 1s delay for eventual consistency
        file_path = None
        for _attempt in range(10):
            if default_storage.exists(storage_key):
                file_path = _resolve_storage_path(storage_key)
                break
            logger.info("import_wizard_plan: waiting for file %s (attempt %d)", storage_key, _attempt + 1)
            time.sleep(1)
        if not file_path:
            raise FileNotFoundError(f"File not found after 10s wait: {storage_key}")
        ext = os.path.splitext(storage_key)[1].lower()

        if ext == ".pdf":
            # PDF plan: extract via OpenAI, then create PlanSnapshot directly
            result = _import_plan_from_pdf(session, file_path)
        else:
            # DOCX plan: use existing operator packet importer
            from apps.public_core.services.operator_packet_importer import import_packet_headless
            result = import_packet_headless(
                file_path=file_path,
                api_number=session.api_number,
                tenant_id=session.tenant_id,
                workspace=session.workspace,
                user_email=session.created_by,
            )

        if result.get("success"):
            # Link the new PlanSnapshot to the session
            plan_snapshot_id = result.get("plan_snapshot_id")
            if plan_snapshot_id:
                from apps.public_core.models import PlanSnapshot
                try:
                    ps = PlanSnapshot.objects.get(pk=plan_snapshot_id)
                    session.plan_snapshot = ps
                except PlanSnapshot.DoesNotExist:
                    logger.warning(
                        "import_wizard_plan: PlanSnapshot %s not found after import",
                        plan_snapshot_id,
                    )

            # Advance status to plan_imported (awaiting user verification)
            session.status = W3WizardSession.STATUS_PLAN_IMPORTED
            session.save(update_fields=["plan_snapshot", "status", "updated_at"])

            logger.info(
                "import_wizard_plan: session %s plan imported successfully, plan_snapshot=%s",
                session_id, plan_snapshot_id,
            )
        else:
            error_msg = result.get("error", "Unknown import error")
            reasons = result.get("reasons", [])
            logger.error(
                "import_wizard_plan: session %s import failed: %s reasons=%s",
                session_id, error_msg, reasons,
            )
            # Store error in parse_result for UI visibility
            session.parse_result = {
                **session.parse_result,
                "plan_import_error": error_msg,
                "plan_import_reasons": reasons,
            }
            session.status = W3WizardSession.STATUS_UPLOADING
            session.save(update_fields=["parse_result", "status", "updated_at"])

    except Exception as exc:
        logger.exception(
            "import_wizard_plan: error processing session %s: %s", session_id, exc
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30)

        session.parse_result = {
            **session.parse_result,
            "plan_import_error": f"Import failed after {self.max_retries} retries: {exc}",
        }
        session.status = W3WizardSession.STATUS_UPLOADING
        session.save(update_fields=["parse_result", "status", "updated_at"])


# ---------------------------------------------------------------------------
# Task 2 — Run plug reconciliation
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=2)
def run_wizard_reconciliation(self, session_id: str) -> None:
    """
    Run plug-plan reconciliation for a W3WizardSession.

    Requires the session to already have a parse_result (STATUS_PARSED).

    Steps:
    1. Load session
    2. Call build_w3_reconciliation(session)
    3. Persist reconciliation_result; advance to STATUS_RECONCILED / step 3
    """
    from apps.public_core.models.w3_wizard_session import W3WizardSession
    from apps.public_core.services.w3_reconciliation_adapter import build_w3_reconciliation

    try:
        session = W3WizardSession.objects.get(pk=session_id)
    except W3WizardSession.DoesNotExist:
        logger.error("run_wizard_reconciliation: session %s not found", session_id)
        return

    try:
        result_dict = build_w3_reconciliation(session)

        session.reconciliation_result = result_dict

        # Pre-populate justifications from AI extraction
        # Clear stale justifications — plug numbers change across reconciliation
        # runs (depth-sort renumbering), so old entries map to wrong plugs.
        # AI suggestions will be re-populated below; engineer must re-enter manual notes.
        existing_justifications = {}
        comparisons = result_dict.get("comparisons", [])
        for comp in comparisons:
            plug_num = str(comp.get("plug_number", ""))
            if not plug_num:
                continue
            # Only create justification entries for divergences shown in the
            # JustificationPanel (major, missing, added) — skip match and minor
            if comp.get("deviation_level") in ("match", "minor"):
                continue
            # Only pre-populate if AI found something AND engineer hasn't written one
            note = comp.get("justification_note", "")
            source_type = comp.get("justification_source_type", "")
            confidence = comp.get("justification_confidence", 0.0)
            source_days = comp.get("justification_source_days", [])
            # Skip if engineer already wrote a custom (non-AI) justification
            existing = existing_justifications.get(plug_num)
            engineer_wrote = (
                existing
                and existing.get("note", "").strip()
                and not existing.get("ai_suggested", False)
            )
            if note and not engineer_wrote:
                existing_justifications[plug_num] = {
                    "note": note,
                    "resolved": False,
                    "resolved_by": "",
                    "resolved_at": None,
                    "ai_suggested": True,
                    "source_type": source_type,
                    "confidence": confidence,
                    "source_days": source_days,
                }
        session.justifications = existing_justifications

        session.status = W3WizardSession.STATUS_RECONCILED
        session.current_step = 3
        session.save(
            update_fields=["reconciliation_result", "justifications", "status", "current_step", "updated_at"]
        )

        # ------------------------------------------------------------------
        # Run formation isolation audit (non-fatal)
        # ------------------------------------------------------------------
        try:
            from apps.public_core.services.formation_isolation_auditor import audit as audit_formations
            from apps.public_core.services.well_geometry_builder import extract_formations_from_payload

            snapshot = session.plan_snapshot
            if snapshot and snapshot.payload:
                payload = snapshot.payload
                formation_tops = extract_formations_from_payload(payload)
                existing_perforations = payload.get("existing_perforations", [])
                if not isinstance(existing_perforations, list):
                    existing_perforations = []
                casing_record = payload.get("casing_record", [])
                if not isinstance(casing_record, list):
                    casing_record = []

                # Get actual plugs from reconciliation comparisons
                # Comparisons use flat keys (actual_top_ft, actual_bottom_ft, etc.)
                actual_plugs = []
                for comp in result_dict.get("comparisons", []):
                    if not isinstance(comp, dict):
                        continue
                    # Skip comparisons with no actual depth data
                    if comp.get("actual_top_ft") is None and comp.get("actual_bottom_ft") is None:
                        continue
                    actual_plugs.append({
                        "plug_number": comp.get("plug_number"),
                        "top_ft": comp.get("actual_top_ft"),
                        "bottom_ft": comp.get("actual_bottom_ft"),
                        "depth_top_ft": comp.get("actual_top_ft"),
                        "depth_bottom_ft": comp.get("actual_bottom_ft"),
                        "sacks": comp.get("actual_sacks"),
                        "cement_class": comp.get("actual_cement_class"),
                        "woc_hours": comp.get("actual_woc_hours"),
                        "woc_tagged": comp.get("actual_woc_tagged"),
                        "tagged_depth_ft": comp.get("actual_tagged_depth_ft"),
                        "placement_method": comp.get("actual_placement_method"),
                        "plug_type": comp.get("actual_type") or comp.get("planned_type"),
                    })

                audit_result = audit_formations(
                    formation_tops=formation_tops,
                    existing_perforations=existing_perforations,
                    casing_record=casing_record,
                    actual_plugs=actual_plugs,
                    api_number=session.api_number,
                )
                from dataclasses import asdict as _asdict
                session.formation_audit = _jsonable(_asdict(audit_result))
                session.save(update_fields=["formation_audit", "updated_at"])
                logger.info(
                    "run_wizard_reconciliation: formation audit complete for %s — status=%s",
                    session_id, audit_result.overall_status,
                )
        except Exception:
            logger.warning(
                "run_wizard_reconciliation: formation audit failed (non-fatal) for %s",
                session_id, exc_info=True,
            )

        # ------------------------------------------------------------------
        # Run COA compliance check (non-fatal)
        # ------------------------------------------------------------------
        try:
            from apps.public_core.services.coa_compliance_checker import check as check_compliance

            compliance = check_compliance(
                reconciliation_result=result_dict,
                parse_result=session.parse_result or {},
                formation_audit=session.formation_audit or {},
                payload=(session.plan_snapshot.payload if session.plan_snapshot else {}),
                api_number=session.api_number,
            )
            from dataclasses import asdict as _asdict2
            session.compliance_result = _jsonable(_asdict2(compliance))
            session.save(update_fields=["compliance_result", "updated_at"])
            logger.info(
                "run_wizard_reconciliation: compliance check complete for %s — status=%s",
                session_id, compliance.overall_status,
            )
        except Exception:
            logger.warning(
                "run_wizard_reconciliation: compliance check failed (non-fatal) for %s",
                session_id, exc_info=True,
            )

        unresolved = result_dict.get("unresolved_divergences", 0)
        logger.info(
            "run_wizard_reconciliation: session %s reconciled, unresolved=%d",
            session_id,
            unresolved,
        )

    except Exception as exc:
        logger.exception(
            "run_wizard_reconciliation: error processing session %s: %s",
            session_id,
            exc,
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30)

        logger.error(
            "run_wizard_reconciliation: session %s failed after max retries", session_id
        )


# ---------------------------------------------------------------------------
# Sundry data adapter (NM jurisdiction)
# ---------------------------------------------------------------------------

def _build_sundry_data(form_dict: dict, session) -> dict:
    """Adapt W3Form dict → sundry_pdf_generator expected format."""
    header = form_dict.get("header", {})
    remarks = form_dict.get("remarks", "")

    # Build plug summary for remarks
    plugs = form_dict.get("plugs", [])
    plug_lines = []
    for p in plugs:
        # Optional placement method prefix: "perf_and_squeeze" → "Perf & Squeeze"
        method_raw = p.get("placement_method")
        if method_raw:
            method_str = method_raw.replace("_", " ").title().replace(" And ", " & ")
            method_prefix = f"{method_str} "
        else:
            method_prefix = ""

        base = (
            f"Plug #{p.get('plug_number')}: {method_prefix}"
            f"{p.get('depth_top_ft', p.get('depth_bottom_ft'))}'-{p.get('depth_bottom_ft')}' "
            f"({p.get('sacks')} sks Class {p.get('cement_class')})"
        )

        # Optional WOC and tagged depth suffixes
        suffix_parts = []
        woc_hours = p.get("woc_hours")
        if woc_hours is not None:
            suffix_parts.append(f"WOC {woc_hours}hrs")
        if p.get("woc_tagged") and p.get("tagged_depth_ft") is not None:
            suffix_parts.append(f"Tagged @ {p.get('tagged_depth_ft')}ft")

        if suffix_parts:
            plug_lines.append(f"{base} — {', '.join(suffix_parts)}")
        else:
            plug_lines.append(base)

    # Wellbore data from plan_snapshot
    wellbore_lines = []
    plan_snapshot = getattr(session, "plan_snapshot", None)
    payload = plan_snapshot.payload if plan_snapshot else {}

    # Casing Record
    casing_record = payload.get("casing_record", [])
    if casing_record:
        wellbore_lines.append("CASING RECORD:")
        for c in casing_record:
            if not isinstance(c, dict):
                continue
            size = c.get("size_inches") or c.get("size") or "?"
            ctype = c.get("casing_type") or c.get("type") or ""
            shoe = c.get("shoe_depth_ft") or c.get("bottom_ft") or "?"
            weight = c.get("weight_ppf") or c.get("weight") or ""
            grade = c.get("grade") or ""
            desc_parts = [f"{size}\" {ctype}".strip()]
            if weight:
                desc_parts.append(f"{weight} ppf")
            if grade:
                desc_parts.append(f"Grade {grade}")
            desc_parts.append(f"to {shoe}'")
            wellbore_lines.append(f"  {' — '.join(desc_parts)}")

    # Formation Tops
    formations = payload.get("formations", [])
    if formations:
        wellbore_lines.append("")
        wellbore_lines.append("FORMATION TOPS:")
        for f in formations:
            if not isinstance(f, dict):
                continue
            name = f.get("formation_name") or f.get("formation") or "Unknown"
            top = f.get("top_ft")
            if top is not None:
                wellbore_lines.append(f"  {name}: {top}'")
            else:
                wellbore_lines.append(f"  {name}: depth unknown")

    # Existing Tools / Equipment in Hole
    well_geometry = payload.get("well_geometry", {})
    existing_tools = []
    if isinstance(well_geometry, dict):
        existing_tools = well_geometry.get("existing_tools", [])
    if existing_tools:
        wellbore_lines.append("")
        wellbore_lines.append("EXISTING TOOLS IN HOLE:")
        for tool in existing_tools:
            if not isinstance(tool, dict):
                continue
            tool_type = tool.get("type") or tool.get("tool_type") or "Unknown"
            depth = tool.get("depth_ft") or tool.get("top_ft") or ""
            desc = tool.get("description") or ""
            if depth:
                wellbore_lines.append(f"  {tool_type} @ {depth}'{f' — {desc}' if desc else ''}")
            else:
                wellbore_lines.append(f"  {tool_type}{f' — {desc}' if desc else ''}")
    else:
        wellbore_lines.append("")
        wellbore_lines.append("NO TOOLS IN HOLE")

    # Perforations
    perforations = payload.get("existing_perforations", [])
    if not perforations and isinstance(well_geometry, dict):
        perforations = well_geometry.get("perforations", []) or well_geometry.get("production_perforations", [])
    if perforations:
        wellbore_lines.append("")
        wellbore_lines.append("PERFORATIONS:")
        for perf in perforations:
            if not isinstance(perf, dict):
                continue
            top = perf.get("top_ft") or perf.get("depth_top_ft") or "?"
            bottom = perf.get("bottom_ft") or perf.get("depth_bottom_ft") or "?"
            formation = perf.get("formation") or ""
            wellbore_lines.append(f"  {top}'-{bottom}'{f' ({formation})' if formation else ''}")

    tenant_name = Tenant.objects.filter(pk=session.tenant_id).values_list("name", flat=True).first() or ""
    operator = header.get("operator", "")
    brief_remarks = (
        f"{tenant_name} respectfully proposes the attached P&A NOI to plug this well on behalf of {operator}.\n"
        f"Please see the attached proposed P&A NOI."
    )

    steps_lines = []
    if wellbore_lines:
        steps_lines.extend(wellbore_lines)
    if plug_lines:
        steps_lines.append("")
        steps_lines.append("PLUGS:")
        steps_lines.extend(plug_lines)
    if remarks:
        steps_lines.append("")
        steps_lines.append(remarks)

    # Formation Isolation Verification section
    formation_audit = getattr(session, "formation_audit", None) or {}
    if formation_audit and formation_audit.get("requirements"):
        steps_lines.append("")
        steps_lines.append("FORMATION ISOLATION VERIFICATION:")
        for req in formation_audit.get("requirements", []):
            if not isinstance(req, dict):
                continue
            status = req.get("status", "unknown").upper()
            label = req.get("label", "")
            formation = req.get("formation_name", "")
            notes = req.get("notes", "")
            if formation:
                steps_lines.append(f"  [{status}] {label} — {formation}: {notes}")
            else:
                steps_lines.append(f"  [{status}] {label}: {notes}")
        overall = formation_audit.get("overall_status", "")
        if overall:
            steps_lines.append(f"  Overall: {overall.upper()}")

    # COA Compliance section
    compliance_result = getattr(session, "compliance_result", None) or {}
    if compliance_result and compliance_result.get("rule_results"):
        steps_lines.append("")
        steps_lines.append("COA COMPLIANCE:")
        pass_count = 0
        for rule in compliance_result.get("rule_results", []):
            if not isinstance(rule, dict):
                continue
            status = rule.get("status", "")
            if status == "pass":
                pass_count += 1
                continue  # Summarize passes
            label = rule.get("rule_label", "")
            detail = rule.get("detail", "")
            steps_lines.append(f"  [{status.upper()}] {label}: {detail}")
        if pass_count > 0:
            steps_lines.append(f"  {pass_count} rule(s) PASSED")
        overall = compliance_result.get("overall_status", "")
        if overall:
            steps_lines.append(f"  Overall: {overall.upper()}")

    # Daily Operations Summary
    parse_result = getattr(session, "parse_result", None) or {}
    days = parse_result.get("days", [])
    if days:
        steps_lines.append("")
        steps_lines.append("DAILY OPERATIONS SUMMARY:")
        for day in days:
            if not isinstance(day, dict):
                continue
            work_date = day.get("work_date", "Unknown date")
            narrative = day.get("daily_narrative", "")
            if narrative:
                # Truncate to 200 chars
                preview = narrative[:200]
                if len(narrative) > 200:
                    preview += "..."
                steps_lines.append(f"  {work_date}: {preview}")

    steps_content = "\n".join(steps_lines)

    wbd_formations = []
    for f in formations:
        if not isinstance(f, dict):
            continue
        name = f.get("formation_name") or f.get("formation") or "Unknown"
        top = f.get("top_ft")
        if top is not None:
            wbd_formations.append({"name": name, "depth_ft": top})

    wbd_casings = []
    for c in casing_record:
        if not isinstance(c, dict):
            continue
        wbd_casings.append({
            "type": c.get("string_type") or c.get("casing_type") or c.get("type") or "",
            "size_inches": c.get("od_in") or c.get("size_inches") or c.get("size") or "",
            "hole_size_inches": c.get("bit_size_in") or c.get("hole_size_in") or "",
            "shoe_depth_ft": c.get("shoe_depth_ft") or c.get("bottom_ft") or "",
        })

    return {
        "header": {
            "api_number": header.get("api_number", session.api_number),
            "well_name": header.get("lease_name", ""),
            "well_number": header.get("well_number", ""),
            "operator": header.get("operator", ""),
            "operator_address": header.get("operator_address", ""),
            "phone": header.get("phone", ""),
            "field_pool": header.get("field_name", ""),
            "location": header.get("section_block_survey", ""),
            "county": header.get("county", ""),
            "state": "NM",
            "lease_serial": header.get("rrc_lease_id", ""),
            "well_type": header.get("well_type", ""),
        },
        "submission_type": "final_abandonment",
        "action_type": "plug_abandon",
        "remarks": brief_remarks,
        "steps_content": steps_content,
        "certification": {
            "name": "",
            "title": "",
            "date": "",
        },
        "wbd_formations": wbd_formations,
        "wbd_casings": wbd_casings,
    }


# ---------------------------------------------------------------------------
# Task 3 — Generate final W-3 form
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=2)
def generate_wizard_w3(self, session_id: str) -> None:
    """
    Generate a W3FormORM record from a reconciled W3WizardSession.

    Steps:
    1. Load session
    2. Validate all MAJOR divergences have resolved justifications
    3. Transition → STATUS_GENERATING
    4. Map parse_result events → W3Event dataclass instances
    5. Build casing state from plan_snapshot
    6. Call build_w3_form() then w3_form_to_dict()
    7. Create W3FormORM and link to session
    8. Transition → STATUS_COMPLETED / step 5
    """
    from apps.public_core.models.w3_wizard_session import W3WizardSession
    from apps.public_core.models.w3_event import W3Event, CasingStringState
    from apps.public_core.models.w3_orm import W3FormORM
    from apps.public_core.services.w3_formatter import build_w3_form, w3_form_to_dict

    try:
        session = W3WizardSession.objects.get(pk=session_id)
    except W3WizardSession.DoesNotExist:
        logger.error("generate_wizard_w3: session %s not found", session_id)
        return

    try:
        # ------------------------------------------------------------------
        # Step 2 — Validate all MAJOR divergences are justified
        # ------------------------------------------------------------------
        reconciliation_result = session.reconciliation_result or {}
        justifications = session.justifications or {}
        comparisons = reconciliation_result.get("comparisons", [])

        unresolved_majors = []
        for comp in comparisons:
            dev_level = comp.get("deviation_level")
            # deviation_level may be a str or a dict with a "value" key
            if isinstance(dev_level, dict):
                dev_level = dev_level.get("value", "")
            if dev_level in ("major", "missing"):
                plug_num = comp.get("plug_number")
                key = str(plug_num) if plug_num is not None else ""
                just = justifications.get(key, {})
                if not just.get("resolved", False):
                    unresolved_majors.append(plug_num)

        if unresolved_majors:
            raise ValueError(
                f"{len(unresolved_majors)} unresolved MAJOR divergence(s) require "
                f"justification before W-3 generation (plug(s): {unresolved_majors})"
            )

        # ------------------------------------------------------------------
        # Step 3 — Transition to generating
        # ------------------------------------------------------------------
        session.status = W3WizardSession.STATUS_GENERATING
        session.save(update_fields=["status", "updated_at"])

        # ------------------------------------------------------------------
        # Step 4 — Map parse_result events → W3Event dataclass instances
        # ------------------------------------------------------------------
        parse_result = session.parse_result or {}
        w3_events: List[W3Event] = []

        for day in parse_result.get("days", []):
            work_date_raw = day.get("work_date")
            if isinstance(work_date_raw, str):
                try:
                    from datetime import datetime as _dt
                    work_date: Optional[date] = _dt.strptime(work_date_raw, "%Y-%m-%d").date()
                except ValueError:
                    work_date = None
            else:
                work_date = work_date_raw  # already a date or None

            for event_dict in day.get("events", []):
                # Parse optional time strings
                def _parse_time(val: Any) -> Optional[dt_time]:
                    if not val:
                        return None
                    if isinstance(val, dt_time):
                        return val
                    try:
                        from datetime import datetime as _dt2
                        return _dt2.strptime(str(val)[:5], "%H:%M").time()
                    except ValueError:
                        return None

                w3_event = W3Event(
                    event_type=event_dict.get("event_type", "other"),
                    date=work_date,
                    api_number=event_dict.get("api_number") or session.api_number,
                    start_time=_parse_time(event_dict.get("start_time")),
                    end_time=_parse_time(event_dict.get("end_time")),
                    depth_top_ft=event_dict.get("depth_top_ft"),
                    depth_bottom_ft=event_dict.get("depth_bottom_ft"),
                    perf_depth_ft=event_dict.get("perf_depth_ft"),
                    tagged_depth_ft=event_dict.get("tagged_depth_ft"),
                    plug_number=event_dict.get("plug_number"),
                    cement_class=event_dict.get("cement_class"),
                    sacks=event_dict.get("sacks"),
                    volume_bbl=event_dict.get("volume_bbl"),
                    pressure_psi=event_dict.get("pressure_psi"),
                    raw_event_detail=event_dict.get("description", ""),
                    work_assignment_id=event_dict.get("work_assignment_id", 0),
                    dwr_id=event_dict.get("dwr_id", 0),
                    jump_to_next_casing=event_dict.get("jump_to_next_casing", False),
                    casing_string=event_dict.get("casing_string"),
                    raw_input_values=event_dict.get("raw_input_values", {}),
                    raw_transformation_rules=event_dict.get("raw_transformation_rules", {}),
                )
                w3_events.append(w3_event)

        # ------------------------------------------------------------------
        # Step 4b — Apply justification overrides to W3Events
        # ------------------------------------------------------------------
        justifications = session.justifications or {}

        # Apply depth/sack corrections from user overrides
        for w3_event in w3_events:
            plug_key = str(w3_event.plug_number)
            entry = justifications.get(plug_key, {})
            if not isinstance(entry, dict):
                continue
            if "depth_top_ft_override" in entry and entry["depth_top_ft_override"] is not None:
                w3_event.depth_top_ft = entry["depth_top_ft_override"]
            if "depth_bottom_ft_override" in entry and entry["depth_bottom_ft_override"] is not None:
                w3_event.depth_bottom_ft = entry["depth_bottom_ft_override"]
            if "sacks_override" in entry and entry["sacks_override"] is not None:
                w3_event.sacks = entry["sacks_override"]

        # Remove events for excluded plugs
        excluded_plugs = set()
        for k, v in justifications.items():
            if isinstance(v, dict) and v.get("excluded"):
                try:
                    excluded_plugs.add(int(k))
                except (ValueError, TypeError):
                    pass
        if excluded_plugs:
            w3_events = [e for e in w3_events if e.plug_number not in excluded_plugs]
            logger.info(
                "generate_wizard_w3: excluded %d plug(s) per justification overrides: %s",
                len(excluded_plugs), excluded_plugs,
            )

        # ------------------------------------------------------------------
        # Step 5 — Build casing state from plan_snapshot (or empty default)
        # ------------------------------------------------------------------
        casing_state: List[CasingStringState] = []

        plan_snapshot = session.plan_snapshot
        if plan_snapshot:
            payload = plan_snapshot.payload or {}
            w3a_form_data = dict(payload)  # shallow copy to avoid mutating stored payload
            # Map well_header → header for PDF generator compatibility
            if "header" not in w3a_form_data and "well_header" in w3a_form_data:
                wh = w3a_form_data["well_header"]
                w3a_form_data["header"] = {
                    "api_number": wh.get("api_number", session.api_number),
                    "rrc_district": wh.get("rrc_district", ""),
                    "rrc_lease_id": wh.get("rrc_lease_id", ""),
                    "field_name": wh.get("field_name", ""),
                    "lease_name": wh.get("lease_name", ""),
                    "well_number": wh.get("well_name", wh.get("well_number", "")),
                    "operator": wh.get("operator", ""),
                    "county": wh.get("county", ""),
                    "operator_address": wh.get("operator_address", ""),
                    "permit_number": wh.get("permit_number", ""),
                    "total_depth": wh.get("total_depth", ""),
                    "original_w1_operator": wh.get("original_w1_operator", ""),
                    "subsequent_w1_operator": wh.get("subsequent_w1_operator", ""),
                    "drilling_permit_date": wh.get("drilling_permit_date", ""),
                    "feet_from_line1": wh.get("feet_from_line1", ""),
                    "feet_from_line2": wh.get("feet_from_line2", ""),
                    "section_block_survey": wh.get("section_block_survey", ""),
                    "direction_from_town": wh.get("direction_from_town", ""),
                    "drilling_commenced": wh.get("drilling_commenced", ""),
                    "drilling_completed": wh.get("drilling_completed", ""),
                    "well_type": wh.get("well_type", ""),
                    "condensate_on_hand": wh.get("condensate_on_hand", ""),
                    "date_well_plugged": wh.get("date_well_plugged", ""),
                    "mud_filled": wh.get("mud_filled"),
                    "all_wells_plugged": wh.get("all_wells_plugged"),
                    "notice_given": wh.get("notice_given"),
                    "mud_application_method": wh.get("mud_application_method", ""),
                    "mud_weight_ppg": wh.get("mud_weight_ppg", ""),
                    "cementing_company": wh.get("cementing_company", ""),
                    "date_rrc_notified": wh.get("date_rrc_notified", ""),
                    "surface_owners": wh.get("surface_owners", ""),
                    "if_no_explain": wh.get("if_no_explain", ""),
                }
            casing_record_raw = payload.get("casing_record", [])
            for casing in casing_record_raw:
                try:
                    casing_state.append(
                        CasingStringState(
                            name=casing.get("string_type", "unknown"),
                            od_in=float(casing.get("size_in") or 0),
                            top_ft=float(casing.get("top_ft") or 0),
                            bottom_ft=float(casing.get("shoe_depth_ft") or casing.get("bottom_ft") or 0),
                            hole_size_in=float(casing.get("hole_size_in")) if casing.get("hole_size_in") else None,
                            removed_to_depth_ft=(
                                float(casing["removed_to_depth_ft"])
                                if casing.get("removed_to_depth_ft") is not None
                                else None
                            ),
                        )
                    )
                except (TypeError, ValueError) as casing_err:
                    logger.warning(
                        "generate_wizard_w3: skipping malformed casing entry %s: %s",
                        casing,
                        casing_err,
                    )
        else:
            w3a_form_data = {}

        # ------------------------------------------------------------------
        # Step 6 — Build W3Form
        # ------------------------------------------------------------------
        w3_form = build_w3_form(w3a_form_data, w3_events, casing_state)

        # ------------------------------------------------------------------
        # Step 7 — Convert to dict
        # ------------------------------------------------------------------
        form_dict = w3_form_to_dict(w3_form)

        # ------------------------------------------------------------------
        # Step 7b — Merge DWR-placed tools into plan_snapshot well_geometry
        # Extract tool-placement events from DWR parse_result and add them to
        # the existing_tools list in the plan_snapshot's well_geometry so the
        # As-Plugged WBD diagram reflects actuals from the daily work reports.
        # ------------------------------------------------------------------
        try:
            dwr_tools: List[Dict[str, Any]] = []
            for day in parse_result.get("days", []):
                day_num = day.get("day_number", "?")
                for event in day.get("events", []):
                    evt_type = event.get("event_type", "")
                    if evt_type == "set_bridge_plug":
                        dwr_tools.append({
                            "source": "dwr_actuals",
                            "tool_type": "CIBP",
                            "depth_ft": event.get("depth_top_ft"),
                            "notes": f"Placed during plugging - Day {day_num}",
                        })
                    elif evt_type in ("set_packer", "set_retainer"):
                        tool_type = "PACKER" if evt_type == "set_packer" else "RETAINER"
                        dwr_tools.append({
                            "source": "dwr_actuals",
                            "tool_type": tool_type,
                            "depth_ft": event.get("depth_top_ft"),
                            "notes": f"Placed during plugging - Day {day_num}",
                        })

            if dwr_tools and session.plan_snapshot:
                payload_ref = session.plan_snapshot.payload or {}
                well_geom = payload_ref.get("well_geometry")
                if well_geom is None:
                    well_geom = {}

                existing = well_geom.get("existing_tools", [])
                if not isinstance(existing, list):
                    existing = []

                # Build a set of (tool_type, depth_ft) tuples already present
                # to avoid double-counting tools from the plan geometry.
                existing_keys = {
                    (
                        (t.get("tool_type") or t.get("type") or "").upper(),
                        t.get("depth_ft") or t.get("top_ft"),
                    )
                    for t in existing
                }

                added = 0
                for tool in dwr_tools:
                    key = (
                        (tool.get("tool_type") or "").upper(),
                        tool.get("depth_ft"),
                    )
                    if key not in existing_keys and tool.get("depth_ft") is not None:
                        existing.append(tool)
                        existing_keys.add(key)
                        added += 1

                if added:
                    well_geom["existing_tools"] = existing
                    payload_ref["well_geometry"] = well_geom
                    session.plan_snapshot.payload = payload_ref
                    session.plan_snapshot.save(update_fields=["payload"])
                    logger.info(
                        "generate_wizard_w3: merged %d DWR tool(s) into plan_snapshot "
                        "well_geometry for session %s",
                        added,
                        session_id,
                    )
        except Exception as tool_merge_err:
            logger.warning(
                "generate_wizard_w3: DWR tool merge failed (non-fatal): %s",
                tool_merge_err,
            )

        # ------------------------------------------------------------------
        # Step 8 — Persist W3FormORM
        # ------------------------------------------------------------------
        new_form = W3FormORM.objects.create(
            well=session.well,
            api_number=session.api_number,
            status="draft",
            form_data=form_dict,
            tenant_id=session.tenant_id,
            workspace=session.workspace,
            auto_generated=True,
        )

        # ------------------------------------------------------------------
        # Step 8b — Write execution components for well lifecycle tracking (non-fatal)
        # ------------------------------------------------------------------
        try:
            from apps.public_core.services.component_writer import write_execution_components
            write_execution_components(session=session, form_dict=form_dict)
        except Exception:
            logger.warning("Failed to write execution components for session %s", session.id, exc_info=True)

        # ------------------------------------------------------------------
        # Step 8c-pre — Log formation audit warning if deficient
        # ------------------------------------------------------------------
        try:
            fa = getattr(session, "formation_audit", None) or {}
            if fa.get("overall_status") == "deficient":
                logger.warning(
                    "generate_wizard_w3: formation audit DEFICIENT for session %s — "
                    "unsatisfied=%d. Report will be generated but may have compliance gaps.",
                    session_id,
                    fa.get("unsatisfied", 0),
                )
        except Exception:
            pass  # Non-fatal informational check

        # ------------------------------------------------------------------
        # Step 8c — Generate PDF (non-fatal)
        # ------------------------------------------------------------------
        pdf_url = None
        try:
            from django.conf import settings as django_settings

            wbd_path = getattr(session, 'wbd_image_path', '') or ''
            if session.jurisdiction == "NM":
                from apps.public_core.services.sundry_pdf_generator import generate_sundry_pdf
                sundry_data = _build_sundry_data(form_dict, session)
                pdf_result = generate_sundry_pdf(sundry_data, wbd_image_path=wbd_path)
            else:
                from apps.public_core.services.w3_pdf_generator import generate_w3_pdf
                pdf_result = generate_w3_pdf(form_dict, wbd_image_path=wbd_path)

            filename = os.path.basename(pdf_result["temp_path"])
            storage_key = f"temp_pdfs/{filename}"

            # Upload the locally-generated PDF to Django's storage backend (S3 or local)
            with open(pdf_result["temp_path"], "rb") as f:
                from django.core.files.base import ContentFile
                default_storage.save(storage_key, ContentFile(f.read()))

            # Clean up local temp file when using remote storage (S3)
            if not hasattr(default_storage, 'path'):
                try:
                    os.unlink(pdf_result["temp_path"])
                except OSError:
                    pass

            # Build URL from storage backend — works for both S3 and local
            pdf_url = default_storage.url(storage_key)
            logger.info("generate_wizard_w3: PDF generated (%s): %s", session.form_type, pdf_url)
        except Exception as pdf_err:
            logger.warning(
                "generate_wizard_w3: PDF generation failed (non-fatal): %s", pdf_err
            )

        # ------------------------------------------------------------------
        # Steps 9-12 — Link form to session, store metadata, advance status
        # ------------------------------------------------------------------
        session.w3_form = new_form
        session.w3_generation_result = {
            "w3_form_id": str(new_form.pk),
            "generated_at": timezone.now().isoformat(),
            "events_processed": len(w3_events),
            "plugs_generated": len(form_dict.get("plugs", [])),
            "pdf_url": pdf_url,
            "form_type": session.form_type,
        }
        session.status = W3WizardSession.STATUS_COMPLETED
        session.current_step = 6
        session.save(
            update_fields=[
                "w3_form",
                "w3_generation_result",
                "status",
                "current_step",
                "updated_at",
            ]
        )

        logger.info(
            "generate_wizard_w3: session %s completed — W3FormORM pk=%s, plugs=%d",
            session_id,
            new_form.pk,
            len(form_dict.get("plugs", [])),
        )

    except ValueError as val_err:
        # Validation errors (unresolved majors) should not be retried
        logger.error(
            "generate_wizard_w3: validation error for session %s: %s",
            session_id,
            val_err,
        )
        raise

    except Exception as exc:
        logger.exception(
            "generate_wizard_w3: unexpected error for session %s: %s", session_id, exc
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30)

        logger.error(
            "generate_wizard_w3: session %s failed after max retries", session_id
        )
