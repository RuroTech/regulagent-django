"""Well ingestion pipeline — TX RRC bulk and NM Water Data API.

Public interface:
    ingest_tx_wells(*, source="all", dry_run=False, limit=None) -> dict
    ingest_nm_wells(*, dry_run=False, limit=None) -> dict

Return shape: {"created": int, "updated": int, "skipped": int, "errors": int, "elapsed_s": float}

IMPORTANT: The module-qualified import style below is required so that test
patches on the module paths intercept correctly.
"""
from __future__ import annotations

import logging
import time

import apps.public_core.tasks_research as _tasks_research_module

from apps.public_core.models import WellRegistry
from apps.public_core.services import nm_wda_client, rrc_bulk_downloader, rrc_wellbore_parser

logger = logging.getLogger(__name__)

# Stable GoAnywhere MFT share UUID for the TX RRC Full Wellbore file
FULL_WELLBORE_UUID = "b070ce28-5c58-4fe2-9eb7-8b70befb7af9"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _upsert_well_row(row: rrc_wellbore_parser.WellRow, dry_run: bool = False) -> tuple[str, WellRegistry | None]:
    """Upsert a TX WellRow into WellRegistry.

    Returns (outcome, well) where outcome is "created" | "updated" | "skipped" | "error".
    """
    try:
        well = WellRegistry.objects.get(api14=row.api14)
        # If the well already has substantive enriched data (operator_name set),
        # skip — the bulk file doesn't carry operator data and we don't want to
        # partially overwrite a well that's already been researched.
        if well.operator_name:
            return "skipped", well
        # Update only if blank fields can be populated
        update_fields = []
        if not well.lat and row.latitude:
            well.lat = row.latitude
            update_fields.append("lat")
        if not well.lon and row.longitude:
            well.lon = row.longitude
            update_fields.append("lon")
        if not well.lease_name and row.lease_name:
            well.lease_name = row.lease_name
            update_fields.append("lease_name")
        if not well.district and row.district:
            well.district = row.district
            update_fields.append("district")
        if update_fields:
            if not dry_run:
                well.save(update_fields=update_fields)
            return "updated", well
        return "skipped", well
    except WellRegistry.DoesNotExist:
        if dry_run:
            return "created", None  # count it but don't write
        well = WellRegistry.objects.create(
            api14=row.api14,
            state="TX",
            county=row.county_code,
            district=row.district,
            lease_name=row.lease_name,
            lat=row.latitude,
            lon=row.longitude,
            data_status="cold_storage",
        )
        return "created", well


def _upsert_nm_well(record: dict, dry_run: bool = False) -> tuple[str, WellRegistry | None]:
    """Upsert an NM well dict into WellRegistry.

    Returns (outcome, well) where outcome is "created" | "updated" | "skipped" | "error".
    """
    api14 = record.get("api14", "")
    if not api14:
        return "skipped", None
    try:
        well = WellRegistry.objects.get(api14=api14)
        update_fields = []
        if not well.operator_name and record.get("operator"):
            well.operator_name = record["operator"]
            update_fields.append("operator_name")
        if not well.lat and record.get("latitude"):
            well.lat = record["latitude"]
            update_fields.append("lat")
        if not well.lon and record.get("longitude"):
            well.lon = record["longitude"]
            update_fields.append("lon")
        if not well.county and record.get("county"):
            well.county = record["county"]
            update_fields.append("county")
        if update_fields:
            if not dry_run:
                well.save(update_fields=update_fields)
            return "updated", well
        return "skipped", well
    except WellRegistry.DoesNotExist:
        if dry_run:
            return "created", None  # count it but don't write
        well = WellRegistry.objects.create(
            api14=api14,
            state="NM",
            operator_name=record.get("operator", ""),
            county=record.get("county", ""),
            lat=record.get("latitude"),
            lon=record.get("longitude"),
            data_status="cold_storage",
        )
        return "created", well


def _dispatch_research(well: WellRegistry) -> None:
    """Dispatch the research pipeline for a newly ingested well.

    Mirrors the FilingSyncer._resolve_well pattern.
    Never raises — ingestion must not fail because research dispatch failed.
    """
    from apps.public_core.models import ResearchSession

    try:
        session = ResearchSession.objects.create(
            api_number=well.api14,
            state=well.state,
            well=well,
            status="pending",
            # No tenant — public ingestion is not tenant-scoped
        )
        # Call via module reference so test patches intercept correctly
        _tasks_research_module.start_research_session_task.delay(str(session.id))
    except Exception as exc:
        logger.warning(
            "well_ingestor: failed to dispatch research for api14=%s: %s",
            well.api14,
            exc,
        )
        # Never fail ingestion because research dispatch failed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ingest_tx_wells(*, source: str = "all", dry_run: bool = False, limit: int | None = None) -> dict:
    """Ingest TX wells from the RRC Full Wellbore bulk file.

    source: "active" | "iwar" | "all"
    Returns: {"created": int, "updated": int, "skipped": int, "errors": int, "elapsed_s": float}
    """
    t0 = time.monotonic()
    counts: dict[str, int] = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}

    # 1. Get file listing
    files = rrc_bulk_downloader.list_share(FULL_WELLBORE_UUID)

    # 2. Select the file to download
    if files:
        # Find the first .gz file, or fall back to row 0
        gz_files = [f for f in files if isinstance(f, dict) and f.get("name", "").endswith(".gz")]
        if not gz_files:
            # Try ShareFile dataclass objects
            gz_files = [f for f in files if hasattr(f, "filename") and f.filename.endswith(".gz")]

        if gz_files:
            selected = gz_files[0]
            if isinstance(selected, dict):
                row_idx = 0
                filename = selected.get("name", "wellbore.gz")
            else:
                row_idx = selected.row_idx
                filename = selected.filename
        else:
            # fallback: use index 0
            selected = files[0]
            if isinstance(selected, dict):
                row_idx = 0
                filename = selected.get("name", "wellbore.gz")
            else:
                row_idx = selected.row_idx
                filename = selected.filename
    else:
        # No files found — still try with defaults (tests may mock empty listing)
        row_idx = 0
        filename = "wellbore.gz"

    # 3. Download to temp file
    path, source_url = rrc_bulk_downloader.download_to_tempfile(
        FULL_WELLBORE_UUID, row_idx, filename
    )

    try:
        # 4. Parse the file
        rows = rrc_wellbore_parser.parse_wellbore_file(path)

        # 5 & 6. Filter and process
        processed = 0
        for row in rows:
            if limit is not None and processed >= limit:
                break

            # Filter by source
            if source == "active" and not row.is_active:
                continue
            elif source == "iwar" and not row.is_iwar:
                continue
            # "all" passes everything through

            # 7. Upsert
            try:
                outcome, well = _upsert_well_row(row, dry_run=dry_run)
                counts[outcome] = counts.get(outcome, 0) + 1
            except Exception as exc:
                logger.warning(
                    "well_ingestor: error upserting TX well api14=%s: %s",
                    row.api14,
                    exc,
                )
                counts["errors"] += 1
                processed += 1
                continue

            # 8. Dispatch research for newly created wells
            if outcome == "created" and not dry_run and well is not None:
                _dispatch_research(well)

            processed += 1

    finally:
        # 9. Cleanup
        rrc_bulk_downloader.cleanup_tempfile(path)

    # 10. Return summary
    counts["elapsed_s"] = time.monotonic() - t0
    return counts


def ingest_nm_wells(*, dry_run: bool = False, limit: int | None = None) -> dict:
    """Ingest NM wells from the NM Water Data API.

    Returns: {"created": int, "updated": int, "skipped": int, "errors": int, "elapsed_s": float}
    """
    t0 = time.monotonic()
    counts: dict[str, int] = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}

    # 1. Instantiate client
    client = nm_wda_client.NMWaterDataClient()

    # 2. Stream records
    processed = 0
    for record in client.list_wells():
        if limit is not None and processed >= limit:
            break

        # 4. Upsert
        try:
            outcome, well = _upsert_nm_well(record, dry_run=dry_run)
            counts[outcome] = counts.get(outcome, 0) + 1
        except Exception as exc:
            logger.warning(
                "well_ingestor: error upserting NM well api14=%s: %s",
                record.get("api14", "?"),
                exc,
            )
            counts["errors"] += 1
            processed += 1
            continue

        # 5. Dispatch research for newly created wells
        if outcome == "created" and not dry_run and well is not None:
            _dispatch_research(well)

        processed += 1

    counts["elapsed_s"] = time.monotonic() - t0
    return counts
