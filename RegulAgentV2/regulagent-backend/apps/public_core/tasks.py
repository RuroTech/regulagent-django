"""
Celery tasks for asynchronous bulk operations on wells and plans.

These tasks handle long-running operations that would timeout in HTTP requests:
- Bulk plan generation
- Bulk status updates
- Bulk data exports
- Well component extraction and population
"""
import logging
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Any
from celery import shared_task
from django.utils import timezone

from apps.tenants.context import set_current_tenant
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def bulk_generate_plans(
    self,
    job_id: str,
    well_ids: List[str],
    options: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Generate plans for multiple wells asynchronously.

    This task:
    1. Iterates through each well_id
    2. Calls the plan generation orchestrator
    3. Updates BulkJob progress after each well
    4. Collects results and errors

    Args:
        job_id: BulkJob UUID
        well_ids: List of API14 well identifiers
        options: Configuration options
            - jurisdiction: Optional jurisdiction override
            - force_regenerate: Force regeneration even if plan exists
            - plugs_mode: "combined", "isolated", or "both"
            - input_mode: "extractions", "user_files", or "hybrid"

    Returns:
        {
            'status': 'success' | 'failed',
            'processed': int,
            'failed': int,
            'results': [
                {
                    'well_id': str,
                    'status': 'success' | 'failed',
                    'plan_id': str (if success),
                    'snapshot_id': str (if success),
                    'error': str (if failed)
                }
            ]
        }
    """
    from apps.public_core.models import BulkJob, WellRegistry
    from apps.public_core.services.w3a_orchestrator import generate_w3a_for_api

    logger.info(f"[BulkTask] Starting bulk_generate_plans for job {job_id}")

    try:
        # Get job and mark as processing
        job = BulkJob.objects.get(id=job_id)
        job.start_processing()
        job.celery_task_id = self.request.id
        job.save(update_fields=['celery_task_id'])

        tenant = Tenant.objects.get(id=job.tenant_id)
        set_current_tenant(tenant)

        logger.info(f"[BulkTask] Job {job_id} marked as processing. Wells to process: {len(well_ids)}")

        results = []
        processed_count = 0
        failed_count = 0

        # Extract options
        jurisdiction = options.get('jurisdiction')
        force_regenerate = options.get('force_regenerate', False)
        plugs_mode = options.get('plugs_mode', 'combined')
        input_mode = options.get('input_mode', 'extractions')

        for well_id in well_ids:
            try:
                logger.info(f"[BulkTask] Processing well {well_id} ({processed_count + failed_count + 1}/{len(well_ids)})")

                # Validate well exists
                try:
                    well = WellRegistry.objects.get(api14=well_id)
                except WellRegistry.DoesNotExist:
                    raise ValueError(f"Well {well_id} not found in registry")

                # Check if plan already exists (unless force_regenerate)
                from apps.public_core.models import PlanSnapshot
                existing_plan = None
                if not force_regenerate:
                    existing_plan = PlanSnapshot.objects.filter(
                        well=well,
                        tenant_id=job.tenant_id,
                        status__in=[
                            PlanSnapshot.STATUS_DRAFT,
                            PlanSnapshot.STATUS_INTERNAL_REVIEW,
                            PlanSnapshot.STATUS_ENGINEER_APPROVED,
                        ]
                    ).first()

                if existing_plan and not force_regenerate:
                    logger.info(f"[BulkTask] Plan already exists for well {well_id}, skipping")
                    results.append({
                        'well_id': well_id,
                        'status': 'skipped',
                        'plan_id': existing_plan.plan_id,
                        'snapshot_id': str(existing_plan.id),
                        'message': 'Plan already exists (use force_regenerate to override)'
                    })
                    processed_count += 1
                    job.increment_progress(success=True)
                    continue

                # Generate plan using orchestrator
                plan_result = generate_w3a_for_api(
                    api_number=well_id,
                    plugs_mode=plugs_mode,
                    input_mode=input_mode,
                    request=None,  # No HTTP request in background task
                    confirm_fact_updates=False,  # Conservative: don't auto-update facts
                    allow_precision_upgrades_only=True,
                )

                if plan_result.get('success'):
                    snapshot_id = plan_result.get('snapshot_id')
                    logger.info(f"[BulkTask] Successfully generated plan for well {well_id}: {snapshot_id}")

                    results.append({
                        'well_id': well_id,
                        'status': 'success',
                        'snapshot_id': snapshot_id,
                        'auto_generated': plan_result.get('auto_generated', True),
                    })
                    processed_count += 1
                    job.increment_progress(success=True)
                else:
                    error_msg = plan_result.get('error', 'Unknown error')
                    logger.warning(f"[BulkTask] Failed to generate plan for well {well_id}: {error_msg}")

                    results.append({
                        'well_id': well_id,
                        'status': 'failed',
                        'error': error_msg
                    })
                    failed_count += 1
                    job.increment_progress(success=False)

            except Exception as e:
                error_msg = str(e)
                logger.exception(f"[BulkTask] Error processing well {well_id}")

                results.append({
                    'well_id': well_id,
                    'status': 'failed',
                    'error': error_msg
                })
                failed_count += 1
                job.increment_progress(success=False)

        # Mark job as complete
        job.result_data = {
            'results': results,
            'summary': {
                'total': len(well_ids),
                'processed': processed_count,
                'failed': failed_count,
            }
        }
        job.complete_successfully()

        logger.info(
            f"[BulkTask] Job {job_id} completed. "
            f"Processed: {processed_count}, Failed: {failed_count}"
        )

        return {
            'status': 'success',
            'processed': processed_count,
            'failed': failed_count,
            'results': results
        }

    except BulkJob.DoesNotExist:
        logger.error(f"[BulkTask] Job {job_id} not found")
        return {
            'status': 'failed',
            'error': f"Job {job_id} not found"
        }

    except Exception as e:
        logger.exception(f"[BulkTask] Fatal error in bulk_generate_plans for job {job_id}")

        # Mark job as failed
        try:
            job = BulkJob.objects.get(id=job_id)
            job.fail(str(e))
        except Exception:
            pass

        # Retry up to 3 times
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=30 * (self.request.retries + 1))

        return {
            'status': 'failed',
            'error': str(e)
        }


@shared_task(bind=True)
def bulk_update_plan_status(
    self,
    job_id: str,
    plan_ids: List[str],
    new_status: str
) -> Dict[str, Any]:
    """
    Update status for multiple plans.

    This task:
    1. Validates status transition for each plan
    2. Updates plan status
    3. Tracks results and errors

    Args:
        job_id: BulkJob UUID
        plan_ids: List of plan_id strings
        new_status: Target status (e.g., 'engineer_approved')

    Returns:
        {
            'status': 'success' | 'failed',
            'processed': int,
            'failed': int,
            'results': [...]
        }
    """
    from apps.public_core.models import BulkJob, PlanSnapshot

    logger.info(f"[BulkTask] Starting bulk_update_plan_status for job {job_id}")

    try:
        # Get job and mark as processing
        job = BulkJob.objects.get(id=job_id)
        job.start_processing()
        job.celery_task_id = self.request.id
        job.save(update_fields=['celery_task_id'])

        logger.info(f"[BulkTask] Job {job_id} processing {len(plan_ids)} plans -> {new_status}")

        results = []
        processed_count = 0
        failed_count = 0

        for plan_id in plan_ids:
            try:
                # Get latest snapshot for this plan_id
                snapshot = PlanSnapshot.objects.filter(
                    plan_id=plan_id,
                    tenant_id=job.tenant_id
                ).order_by('-created_at').first()

                if not snapshot:
                    raise ValueError(f"Plan {plan_id} not found")

                # Validate transition (basic validation)
                if snapshot.status == new_status:
                    logger.info(f"[BulkTask] Plan {plan_id} already in status {new_status}")
                    results.append({
                        'plan_id': plan_id,
                        'status': 'skipped',
                        'message': f'Already in status {new_status}'
                    })
                    processed_count += 1
                    job.increment_progress(success=True)
                    continue

                # Update status
                old_status = snapshot.status
                snapshot.status = new_status
                snapshot.save(update_fields=['status'])

                logger.info(f"[BulkTask] Updated plan {plan_id}: {old_status} -> {new_status}")

                results.append({
                    'plan_id': plan_id,
                    'status': 'success',
                    'old_status': old_status,
                    'new_status': new_status
                })
                processed_count += 1
                job.increment_progress(success=True)

            except Exception as e:
                error_msg = str(e)
                logger.warning(f"[BulkTask] Failed to update plan {plan_id}: {error_msg}")

                results.append({
                    'plan_id': plan_id,
                    'status': 'failed',
                    'error': error_msg
                })
                failed_count += 1
                job.increment_progress(success=False)

        # Mark job as complete
        job.result_data = {
            'results': results,
            'summary': {
                'total': len(plan_ids),
                'processed': processed_count,
                'failed': failed_count,
            }
        }
        job.complete_successfully()

        logger.info(
            f"[BulkTask] Job {job_id} completed. "
            f"Processed: {processed_count}, Failed: {failed_count}"
        )

        return {
            'status': 'success',
            'processed': processed_count,
            'failed': failed_count,
            'results': results
        }

    except BulkJob.DoesNotExist:
        logger.error(f"[BulkTask] Job {job_id} not found")
        return {
            'status': 'failed',
            'error': f"Job {job_id} not found"
        }

    except Exception as e:
        logger.exception(f"[BulkTask] Fatal error in bulk_update_plan_status for job {job_id}")

        # Mark job as failed
        try:
            job = BulkJob.objects.get(id=job_id)
            job.fail(str(e))
        except Exception:
            pass

        return {
            'status': 'failed',
            'error': str(e)
        }


def _safe_decimal(val) -> Decimal | None:
    """Convert a value to Decimal, returning None on any failure."""
    if val is None:
        return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, TypeError, ValueError):
        return None


@shared_task(bind=True, max_retries=3)
def extract_and_populate_components(self, api14: str, tenant_id: str = None):
    """
    Extract well data from RRC/OCD and write WellComponent(layer='public') records.
    Called during bulk well import to populate component data for a well.
    """
    from django.db import transaction
    from apps.public_core.models import WellRegistry, WellComponent, WellComponentSnapshot
    from apps.public_core.models.extracted_document import ExtractedDocument
    from apps.public_core.models.public_casing_string import PublicCasingString
    from apps.public_core.models.public_perforation import PublicPerforation
    from apps.public_core.services.rrc_completions_extractor import extract_completions_all_documents

    logger.info(f"[ExtractTask] Starting extract_and_populate_components for api14={api14}")

    try:
        # 1. Get or create WellRegistry
        well, created = WellRegistry.objects.get_or_create(api14=api14)
        if created:
            logger.info(f"[ExtractTask] Created new WellRegistry for api14={api14}")

        # 2. Idempotency — per-document check moved into extraction loop below

        # 3. Download and extract documents from RRC
        logger.info(f"[ExtractTask] Running extract_completions_all_documents for api14={api14}")
        extract_completions_all_documents(api14)

        # 4. Query ExtractedDocument records for this well
        # Primary: join through WellRegistry (EDs linked by well FK)
        extracted_docs = ExtractedDocument.objects.filter(well__api14=api14, status='success')

        # Fallback: suffix match on api10 (strip trailing 0000 from api14)
        if not extracted_docs.exists():
            import re as _re
            clean = _re.sub(r"\D+", "", api14)
            # Use API10 portion for suffix (last 8 of 10-digit, not 14-digit)
            api10 = clean[:10] if len(clean) == 14 else clean
            if len(api10) >= 8:
                suffix = api10[-8:]
                candidate_docs = ExtractedDocument.objects.filter(status='success').only('id', 'api_number')
                matched_ids = []
                for ed in candidate_docs.iterator():
                    ed_clean = _re.sub(r"\D+", "", str(ed.api_number or ""))
                    if len(ed_clean) >= 8 and ed_clean[-8:] == suffix:
                        matched_ids.append(ed.id)
                if matched_ids:
                    extracted_docs = ExtractedDocument.objects.filter(id__in=matched_ids, status='success')
                    logger.info(f"[ExtractTask] Suffix match found {len(matched_ids)} docs for api14={api14}")

        logger.info(f"[ExtractTask] Found {extracted_docs.count()} extracted documents for api14={api14}")

        components: list[WellComponent] = []

        for doc in extracted_docs:
            # Per-document idempotency: skip if components already extracted from this doc
            if WellComponent.objects.filter(
                well=well,
                layer=WellComponent.Layer.PUBLIC,
                provenance__extracted_document_id=str(doc.id),
            ).exists():
                logger.info(f"[ExtractTask] Components already exist for doc {doc.id}, skipping")
                continue
            json_data = doc.json_data or {}
            doc_type = doc.document_type.lower()

            # ----------------------------------------------------------------
            # W-2: casing, formation tops, tubing, liners
            # ----------------------------------------------------------------
            if doc_type == 'w2':
                # Casing strings
                for record in json_data.get('casing_record', []) or []:
                    string_type = record.get('string_type', '') or ''
                    comp_type = (
                        WellComponent.ComponentType.LINER
                        if 'liner' in string_type.lower()
                        else WellComponent.ComponentType.CASING
                    )
                    # Infer hole_size_in from casing OD if not in source doc
                    hole_size = _safe_decimal(record.get('hole_size_in'))
                    if hole_size is None:
                        od = _safe_decimal(record.get('size_in'))
                        if od is not None:
                            from apps.public_core.services.well_geometry_builder import _infer_hole_size
                            hole_size = _safe_decimal(_infer_hole_size(float(od)))

                    components.append(WellComponent(
                        well=well,
                        component_type=comp_type,
                        layer=WellComponent.Layer.PUBLIC,
                        lifecycle_state=WellComponent.LifecycleState.INSTALLED,
                        outside_dia_in=_safe_decimal(record.get('size_in')),
                        weight_ppf=_safe_decimal(record.get('weight_ppf')),
                        grade=record.get('grade', '') or '',
                        bottom_ft=_safe_decimal(record.get('shoe_depth_ft')),
                        top_ft=_safe_decimal(record.get('top_ft')),
                        hole_size_in=hole_size,
                        cement_top_ft=_safe_decimal(record.get('cement_top_ft')),
                        source_document_type='w2',
                        provenance={'extracted_document_id': str(doc.id)},
                        properties={'string_type': string_type},
                    ))

                # Formation tops
                for record in json_data.get('formation_record', []) or []:
                    props = {}
                    if record.get('formation'):
                        props['formation'] = record['formation']
                    components.append(WellComponent(
                        well=well,
                        component_type=WellComponent.ComponentType.FORMATION_TOP,
                        layer=WellComponent.Layer.PUBLIC,
                        lifecycle_state=WellComponent.LifecycleState.INSTALLED,
                        top_ft=_safe_decimal(record.get('top_ft')),
                        source_document_type='w2',
                        provenance={'extracted_document_id': str(doc.id)},
                        properties=props,
                    ))

                # Tubing
                for record in json_data.get('tubing_record', []) or []:
                    components.append(WellComponent(
                        well=well,
                        component_type=WellComponent.ComponentType.TUBING,
                        layer=WellComponent.Layer.PUBLIC,
                        lifecycle_state=WellComponent.LifecycleState.INSTALLED,
                        outside_dia_in=_safe_decimal(record.get('size_in')),
                        top_ft=_safe_decimal(record.get('top_ft')),
                        bottom_ft=_safe_decimal(record.get('bottom_ft') or record.get('shoe_depth_ft')),
                        source_document_type='w2',
                        provenance={'extracted_document_id': str(doc.id)},
                        properties={},
                    ))

                # Liner records (explicit liner section)
                for record in json_data.get('liner_record', []) or []:
                    components.append(WellComponent(
                        well=well,
                        component_type=WellComponent.ComponentType.LINER,
                        layer=WellComponent.Layer.PUBLIC,
                        lifecycle_state=WellComponent.LifecycleState.INSTALLED,
                        outside_dia_in=_safe_decimal(record.get('size_in')),
                        top_ft=_safe_decimal(record.get('top_ft')),
                        bottom_ft=_safe_decimal(record.get('bottom_ft') or record.get('shoe_depth_ft')),
                        hole_size_in=_safe_decimal(record.get('hole_size_in')),
                        cement_top_ft=_safe_decimal(record.get('cement_top_ft')),
                        source_document_type='w2',
                        provenance={'extracted_document_id': str(doc.id)},
                        properties={},
                    ))

            # ----------------------------------------------------------------
            # W-15: perforations, mechanical equipment, historic cement jobs
            # ----------------------------------------------------------------
            elif doc_type == 'w15':
                # Perforations
                for record in json_data.get('perforations', []) or []:
                    props = {}
                    if record.get('formation'):
                        props['formation'] = record['formation']
                    components.append(WellComponent(
                        well=well,
                        component_type=WellComponent.ComponentType.PERFORATION,
                        layer=WellComponent.Layer.PUBLIC,
                        lifecycle_state=WellComponent.LifecycleState.INSTALLED,
                        top_ft=_safe_decimal(record.get('top_ft')),
                        bottom_ft=_safe_decimal(record.get('bottom_ft')),
                        source_document_type='w15',
                        provenance={'extracted_document_id': str(doc.id)},
                        properties=props,
                    ))

                # Mechanical equipment
                _EQUIPMENT_TYPE_MAP = {
                    'cibp': WellComponent.ComponentType.BRIDGE_PLUG,
                    'bridge_plug': WellComponent.ComponentType.BRIDGE_PLUG,
                    'packer': WellComponent.ComponentType.PACKER,
                    'cement_plug': WellComponent.ComponentType.CEMENT_PLUG,
                }
                for record in json_data.get('mechanical_equipment', []) or []:
                    raw_equip = (record.get('equipment_type') or '').lower()
                    comp_type = _EQUIPMENT_TYPE_MAP.get(raw_equip)
                    if comp_type is None:
                        continue
                    props = {}
                    if record.get('sacks') is not None:
                        props['sacks'] = record['sacks']
                    if record.get('notes'):
                        props['notes'] = record['notes']
                    components.append(WellComponent(
                        well=well,
                        component_type=comp_type,
                        layer=WellComponent.Layer.PUBLIC,
                        lifecycle_state=WellComponent.LifecycleState.INSTALLED,
                        depth_ft=_safe_decimal(record.get('depth_ft')),
                        source_document_type='w15',
                        provenance={'extracted_document_id': str(doc.id)},
                        properties=props,
                    ))

                # Historic cement jobs
                for record in json_data.get('historic_cement_jobs', []) or []:
                    components.append(WellComponent(
                        well=well,
                        component_type=WellComponent.ComponentType.CEMENT_JOB,
                        layer=WellComponent.Layer.PUBLIC,
                        lifecycle_state=WellComponent.LifecycleState.INSTALLED,
                        top_ft=_safe_decimal(record.get('interval_top_ft')),
                        bottom_ft=_safe_decimal(record.get('interval_bottom_ft')),
                        sacks=_safe_decimal(record.get('sacks')),
                        cement_class=record.get('cement_class') or '',
                        source_document_type='w15',
                        provenance={'extracted_document_id': str(doc.id)},
                        properties={},
                    ))

            # ----------------------------------------------------------------
            # C-105 (NM): casing, formation tops (equivalent of W-2)
            # ----------------------------------------------------------------
            elif doc_type in ('c_105', 'c105'):
                # Casing strings
                for record in json_data.get('casing_record', []) or []:
                    string_type = record.get('string_type', '') or ''
                    comp_type = (
                        WellComponent.ComponentType.LINER
                        if 'liner' in string_type.lower()
                        else WellComponent.ComponentType.CASING
                    )
                    # Infer hole_size_in from casing OD if not in source doc
                    hole_size = _safe_decimal(record.get('hole_size_in'))
                    if hole_size is None:
                        od = _safe_decimal(record.get('size_in'))
                        if od is not None:
                            from apps.public_core.services.well_geometry_builder import _infer_hole_size
                            hole_size = _safe_decimal(_infer_hole_size(float(od)))

                    components.append(WellComponent(
                        well=well,
                        component_type=comp_type,
                        layer=WellComponent.Layer.PUBLIC,
                        lifecycle_state=WellComponent.LifecycleState.INSTALLED,
                        outside_dia_in=_safe_decimal(record.get('size_in')),
                        bottom_ft=_safe_decimal(record.get('bottom_ft') or record.get('shoe_depth_ft') or record.get('depth_ft')),
                        top_ft=_safe_decimal(record.get('top_ft')),
                        hole_size_in=hole_size,
                        weight_ppf=_safe_decimal(record.get('weight_ppf')),
                        grade=record.get('grade', '') or '',
                        cement_top_ft=_safe_decimal(record.get('cement_top_ft')),
                        source_document_type='c_105',
                        provenance={'extracted_document_id': str(doc.id)},
                        properties={'string_type': string_type},
                    ))

                # Formation tops
                for record in json_data.get('formation_record', []) or []:
                    props = {}
                    if record.get('formation'):
                        props['formation'] = record['formation']
                    components.append(WellComponent(
                        well=well,
                        component_type=WellComponent.ComponentType.FORMATION_TOP,
                        layer=WellComponent.Layer.PUBLIC,
                        lifecycle_state=WellComponent.LifecycleState.INSTALLED,
                        top_ft=_safe_decimal(record.get('top_ft')),
                        source_document_type='c_105',
                        provenance={'extracted_document_id': str(doc.id)},
                        properties=props,
                    ))

                # Perforations (C-105 perforation_record)
                for record in json_data.get('perforation_record', []) or []:
                    props = {}
                    if record.get('formation'):
                        props['formation'] = record['formation']
                    components.append(WellComponent(
                        well=well,
                        component_type=WellComponent.ComponentType.PERFORATION,
                        layer=WellComponent.Layer.PUBLIC,
                        lifecycle_state=WellComponent.LifecycleState.INSTALLED,
                        top_ft=_safe_decimal(record.get('top_ft') or record.get('interval_top_ft')),
                        bottom_ft=_safe_decimal(record.get('bottom_ft') or record.get('interval_bottom_ft')),
                        source_document_type='c_105',
                        provenance={'extracted_document_id': str(doc.id)},
                        properties=props,
                    ))

            # ----------------------------------------------------------------
            # C-101 (NM): casing records (Application for Permit to Drill)
            # ----------------------------------------------------------------
            elif doc_type in ('c_101', 'c101'):
                for record in json_data.get('casing_record', json_data.get('casing_program', [])) or []:
                    string_type = record.get('string_type', '') or ''
                    comp_type = (
                        WellComponent.ComponentType.LINER
                        if 'liner' in string_type.lower()
                        else WellComponent.ComponentType.CASING
                    )
                    components.append(WellComponent(
                        well=well,
                        component_type=comp_type,
                        layer=WellComponent.Layer.PUBLIC,
                        lifecycle_state=WellComponent.LifecycleState.INSTALLED,
                        outside_dia_in=_safe_decimal(record.get('size_in')),
                        bottom_ft=_safe_decimal(record.get('bottom_ft') or record.get('setting_depth_ft') or record.get('shoe_depth_ft')),
                        top_ft=_safe_decimal(record.get('top_ft')),
                        hole_size_in=_safe_decimal(record.get('hole_size_in')),
                        weight_ppf=_safe_decimal(record.get('weight_ppf')),
                        cement_top_ft=_safe_decimal(record.get('cement_top_ft')),
                        source_document_type='c_101',
                        provenance={'extracted_document_id': str(doc.id)},
                        properties={'string_type': string_type},
                    ))

            # ----------------------------------------------------------------
            # C-103 (NM): plugging records, casing, perforations
            # ----------------------------------------------------------------
            elif doc_type in ('c_103', 'c103'):
                # Casing from casing_program
                for record in json_data.get('casing_program', []) or []:
                    if not isinstance(record, dict):
                        continue
                    string_type = record.get('string_type', '') or ''
                    comp_type = (
                        WellComponent.ComponentType.LINER
                        if 'liner' in string_type.lower()
                        else WellComponent.ComponentType.CASING
                    )
                    components.append(WellComponent(
                        well=well,
                        component_type=comp_type,
                        layer=WellComponent.Layer.PUBLIC,
                        lifecycle_state=WellComponent.LifecycleState.INSTALLED,
                        outside_dia_in=_safe_decimal(record.get('size_in')),
                        bottom_ft=_safe_decimal(record.get('bottom_ft') or record.get('setting_depth_ft') or record.get('shoe_depth_ft')),
                        top_ft=_safe_decimal(record.get('top_ft')),
                        hole_size_in=_safe_decimal(record.get('hole_size_in')),
                        weight_ppf=_safe_decimal(record.get('weight_ppf')),
                        source_document_type='c_103',
                        provenance={'extracted_document_id': str(doc.id)},
                        properties={'string_type': string_type},
                    ))

                # Plug records
                for record in json_data.get('plug_record', []) or []:
                    plug_type_raw = (record.get('plug_type') or record.get('material') or 'cement').lower()
                    if 'bridge' in plug_type_raw:
                        comp_type = WellComponent.ComponentType.BRIDGE_PLUG
                    else:
                        comp_type = WellComponent.ComponentType.CEMENT_PLUG
                    components.append(WellComponent(
                        well=well,
                        component_type=comp_type,
                        layer=WellComponent.Layer.PUBLIC,
                        lifecycle_state=WellComponent.LifecycleState.INSTALLED,
                        top_ft=_safe_decimal(record.get('depth_top_ft') or record.get('top_ft')),
                        bottom_ft=_safe_decimal(record.get('depth_bottom_ft') or record.get('bottom_ft')),
                        sacks=_safe_decimal(record.get('sacks')),
                        cement_class=record.get('cement_class') or '',
                        source_document_type='c_103',
                        provenance={'extracted_document_id': str(doc.id)},
                        properties={},
                    ))

                # Perforations
                for record in json_data.get('perforations', []) or []:
                    props = {}
                    if record.get('formation'):
                        props['formation'] = record['formation']
                    components.append(WellComponent(
                        well=well,
                        component_type=WellComponent.ComponentType.PERFORATION,
                        layer=WellComponent.Layer.PUBLIC,
                        lifecycle_state=WellComponent.LifecycleState.INSTALLED,
                        top_ft=_safe_decimal(record.get('top_ft')),
                        bottom_ft=_safe_decimal(record.get('bottom_ft')),
                        source_document_type='c_103',
                        provenance={'extracted_document_id': str(doc.id)},
                        properties=props,
                    ))

            # ----------------------------------------------------------------
            # W-3 / W-3A: plug records from filed forms
            # ----------------------------------------------------------------
            elif doc_type in ('w3', 'w3a'):
                # Plug records
                for record in json_data.get('plug_record', []) or []:
                    plug_type_raw = (record.get('plug_type') or record.get('material') or 'cement').lower()
                    if 'bridge' in plug_type_raw:
                        comp_type = WellComponent.ComponentType.BRIDGE_PLUG
                    else:
                        comp_type = WellComponent.ComponentType.CEMENT_PLUG
                    components.append(WellComponent(
                        well=well,
                        component_type=comp_type,
                        layer=WellComponent.Layer.PUBLIC,
                        lifecycle_state=WellComponent.LifecycleState.INSTALLED,
                        top_ft=_safe_decimal(record.get('depth_top_ft') or record.get('top_ft')),
                        bottom_ft=_safe_decimal(record.get('depth_bottom_ft') or record.get('bottom_ft')),
                        sacks=_safe_decimal(record.get('sacks')),
                        cement_class=record.get('cement_class') or '',
                        source_document_type=doc_type,
                        provenance={'extracted_document_id': str(doc.id)},
                        properties={},
                    ))

        # ── Deduplicate casing/liner by string_type, keeping most authoritative source ──
        _DOC_AUTHORITY = {'c_105': 1, 'w2': 2, 'c_103': 3, 'w15': 4, 'c_101': 5, 'w3': 6, 'w3a': 7}

        deduped_components = []
        seen_casing_keys = {}  # key = (component_type, string_type) → best authority

        # First pass: find best authority for each casing/liner string_type
        for comp in components:
            if comp.component_type in (WellComponent.ComponentType.CASING, WellComponent.ComponentType.LINER):
                string_type = (comp.properties or {}).get('string_type', '').lower()
                key = (comp.component_type, string_type)
                auth = _DOC_AUTHORITY.get(comp.source_document_type, 99)
                if key not in seen_casing_keys or auth < seen_casing_keys[key]:
                    seen_casing_keys[key] = auth

        # Second pass: keep only the best-authority casing/liner, pass through everything else
        seen_casing_added = set()
        for comp in components:
            if comp.component_type in (WellComponent.ComponentType.CASING, WellComponent.ComponentType.LINER):
                string_type = (comp.properties or {}).get('string_type', '').lower()
                key = (comp.component_type, string_type)
                auth = _DOC_AUTHORITY.get(comp.source_document_type, 99)
                if auth == seen_casing_keys.get(key) and key not in seen_casing_added:
                    deduped_components.append(comp)
                    seen_casing_added.add(key)
                # else skip this duplicate
            else:
                deduped_components.append(comp)

        if len(deduped_components) != len(components):
            logger.info(
                f"[ExtractTask] Deduplication: {len(components)} → {len(deduped_components)} "
                f"(removed {len(components) - len(deduped_components)} duplicate casings)"
            )
        components = deduped_components

        # ── Deduplicate perforations and formation tops by depth range (±5 ft tolerance) ──
        _TOLERANCE_FT = 5
        final_components = []
        seen_perf_ranges = []  # list of (top, bottom) tuples
        seen_formation_depths = []  # list of top_ft values

        for comp in components:
            if comp.component_type == WellComponent.ComponentType.PERFORATION:
                t = float(comp.top_ft) if comp.top_ft is not None else None
                b = float(comp.bottom_ft) if comp.bottom_ft is not None else None
                if t is not None and b is not None:
                    is_dup = any(
                        abs(t - st) <= _TOLERANCE_FT and abs(b - sb) <= _TOLERANCE_FT
                        for st, sb in seen_perf_ranges
                    )
                    if is_dup:
                        continue
                    seen_perf_ranges.append((t, b))
                final_components.append(comp)
            elif comp.component_type == WellComponent.ComponentType.FORMATION_TOP:
                t = float(comp.top_ft) if comp.top_ft is not None else None
                fname = (comp.properties or {}).get('formation', '').lower()
                if t is not None:
                    is_dup = any(
                        abs(t - sd) <= _TOLERANCE_FT and fname == sf
                        for sd, sf in seen_formation_depths
                    )
                    if is_dup:
                        continue
                    seen_formation_depths.append((t, fname))
                final_components.append(comp)
            else:
                final_components.append(comp)

        if len(final_components) != len(components):
            logger.info(
                f"[ExtractTask] Perf/formation dedup: {len(components)} → {len(final_components)} "
                f"(removed {len(components) - len(final_components)} duplicates)"
            )
        components = final_components

        # 5. Include PublicCasingString / PublicPerforation (same as backfill command)
        for cs in PublicCasingString.objects.filter(well=well):
            components.append(WellComponent(
                well=well,
                component_type=WellComponent.ComponentType.CASING,
                layer=WellComponent.Layer.PUBLIC,
                lifecycle_state=WellComponent.LifecycleState.INSTALLED,
                sort_order=cs.string_no,
                outside_dia_in=cs.outside_dia_in,
                weight_ppf=cs.weight_ppf,
                grade=cs.grade or '',
                thread_type=cs.thread_type or '',
                top_ft=cs.top_ft,
                bottom_ft=cs.shoe_ft,
                cement_top_ft=cs.cement_to_ft,
                provenance=cs.provenance,
                source_document_type=cs.source or '',
                as_of=cs.as_of,
                properties={'string_no': cs.string_no},
            ))

        for perf in PublicPerforation.objects.filter(well=well):
            perf_props: dict = {}
            if perf.formation:
                perf_props['formation'] = perf.formation
            if perf.shot_density_spf is not None:
                perf_props['shot_density_spf'] = float(perf.shot_density_spf)
            if perf.phase_deg is not None:
                perf_props['phase_deg'] = float(perf.phase_deg)
            components.append(WellComponent(
                well=well,
                component_type=WellComponent.ComponentType.PERFORATION,
                layer=WellComponent.Layer.PUBLIC,
                lifecycle_state=WellComponent.LifecycleState.INSTALLED,
                top_ft=perf.top_ft,
                bottom_ft=perf.bottom_ft,
                provenance=perf.provenance,
                source_document_type=perf.source or '',
                as_of=perf.as_of,
                properties=perf_props,
            ))

        logger.info(f"[ExtractTask] Prepared {len(components)} WellComponent records for api14={api14}")

        # 6. Bulk create all components
        with transaction.atomic():
            WellComponent.objects.bulk_create(components, ignore_conflicts=True)

        # 7. Create baseline snapshot
        snapshot_data = [
            {
                'component_type': c.component_type,
                'top_ft': float(c.top_ft) if c.top_ft is not None else None,
                'bottom_ft': float(c.bottom_ft) if c.bottom_ft is not None else None,
                'depth_ft': float(c.depth_ft) if c.depth_ft is not None else None,
                'outside_dia_in': float(c.outside_dia_in) if c.outside_dia_in is not None else None,
                'source_document_type': c.source_document_type,
                'properties': c.properties,
            }
            for c in components
        ]
        WellComponentSnapshot.objects.create(
            well=well,
            tenant_id=tenant_id,
            context=WellComponentSnapshot.SnapshotContext.BASELINE,
            snapshot_data=snapshot_data,
            component_count=len(components),
        )

        logger.info(
            f"[ExtractTask] Completed extract_and_populate_components for api14={api14}. "
            f"Created {len(components)} components."
        )
        return {'status': 'success', 'api14': api14, 'component_count': len(components)}

    except Exception as e:
        logger.exception(f"[ExtractTask] Error in extract_and_populate_components for api14={api14}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=30 * (self.request.retries + 1))
        return {'status': 'failed', 'api14': api14, 'error': str(e)}


@shared_task(bind=True)
def bulk_import_wells(
    self,
    job_id: str,
    api_numbers: List[str],
    tenant_id: str,
    workspace_id=None,
) -> Dict[str, Any]:
    """
    Import wells in bulk: get-or-create WellRegistry, track engagement,
    then run extraction synchronously per well.

    Args:
        job_id: BulkJob UUID string
        api_numbers: List of API-14 strings
        tenant_id: Tenant UUID string
        workspace_id: Optional workspace ID (unused for now, reserved for future filtering)
    """
    from apps.public_core.models import BulkJob, WellRegistry
    from apps.tenant_overlay.services.engagement_tracker import track_well_interaction
    from uuid import UUID

    logger.info(f"[BulkImport] Starting bulk_import_wells for job {job_id}, {len(api_numbers)} wells")

    try:
        job = BulkJob.objects.get(id=job_id)
        job.start_processing()
        job.celery_task_id = self.request.id
        job.save(update_fields=['celery_task_id'])

        tenant_uuid = UUID(tenant_id)
        results = []
        processed_count = 0
        failed_count = 0

        for api14 in api_numbers:
            try:
                well, created = WellRegistry.objects.get_or_create(
                    api14=api14,
                    defaults={'state': api14[:2]},
                )
                if created:
                    logger.info(f"[BulkImport] Created new WellRegistry for api14={api14}")

                track_well_interaction(tenant_uuid, well, "well_imported")

                # Synchronous extraction within task
                extract_result = extract_and_populate_components(api14, tenant_id)

                results.append({
                    'api14': api14,
                    'status': 'success',
                    'well_created': created,
                    'extract_result': extract_result,
                })
                processed_count += 1
                job.increment_progress(success=True)

            except Exception as e:
                error_msg = str(e)
                logger.exception(f"[BulkImport] Error processing api14={api14}: {error_msg}")
                results.append({
                    'api14': api14,
                    'status': 'failed',
                    'error': error_msg,
                })
                failed_count += 1
                job.increment_progress(success=False)

        job.result_data = {
            'results': results,
            'summary': {
                'total': len(api_numbers),
                'processed': processed_count,
                'failed': failed_count,
            },
        }
        job.complete_successfully()

        logger.info(
            f"[BulkImport] Job {job_id} complete. "
            f"Processed: {processed_count}, Failed: {failed_count}"
        )

        return {
            'status': 'success',
            'processed': processed_count,
            'failed': failed_count,
            'results': results,
        }

    except BulkJob.DoesNotExist:
        logger.error(f"[BulkImport] Job {job_id} not found")
        return {'status': 'failed', 'error': f"Job {job_id} not found"}

    except Exception as e:
        logger.exception(f"[BulkImport] Fatal error in bulk_import_wells for job {job_id}")
        try:
            job = BulkJob.objects.get(id=job_id)
            job.fail(str(e))
        except Exception:
            pass
        return {'status': 'failed', 'error': str(e)}
