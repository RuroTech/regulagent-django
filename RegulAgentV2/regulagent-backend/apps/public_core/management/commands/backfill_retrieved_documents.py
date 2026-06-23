"""
Management command: backfill_retrieved_documents

Backfills RetrievedDocument manifest rows for wells researched BEFORE the
manifest feature shipped.  These wells show "Documents (0)" because they have
existing ExtractedDocuments and on-disk PDFs but no corresponding
RetrievedDocument rows.

The command is fully idempotent — safe to re-run.  All writes go through
update_or_create keyed on (api_number, href).

Sources processed (both per-well, unioned)
------------------------------------------
A. Existing ExtractedDocuments for that api_number:
   - Creates/updates a RetrievedDocument linked to the ED.
   - index_status mirrors ed.status; extracted_document FK is set.
   - filename / local_path pulled from ed.source_path (for RRC-sourced EDs)
     or ed.neubus_filename (for Neubus EDs).
   - kind = ed.document_type (kept in underscore format, e.g. "c_105").

B. Downloaded PDF files on disk under MEDIA_ROOT/rrc/completions/<api>/ that
   have NO matching ExtractedDocument:
   - Creates a RetrievedDocument with index_status="pending" normally, or
     "skipped_directional" when the filename matches the directional-survey
     pattern ('directional' AND 'survey' in name.lower()).
   - local_path = absolute file path; file_hash = SHA-256 (computed cheaply).

Usage
-----
    python manage.py backfill_retrieved_documents [--well <api14>] [--dry-run]

Summary
-------
    Wells processed: N
    ED-sourced   created: X  updated/linked: Y  skipped: Z
    Disk-sourced created: A  skipped (no orphan): B  (B may be 0 for old wells)
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.public_core.models import (
    ExtractedDocument,
    RetrievedDocument,
    WellRegistry,
)
from apps.public_core.services.api_normalization import normalize_api_14digit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _media_completions_root() -> Path:
    """Mirror of rrc_completions_extractor._media_base()."""
    base = getattr(settings, "MEDIA_ROOT", None)
    return Path(base or ".").resolve() / "rrc" / "completions"


def _sha256_of_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _is_directional(filename: str) -> bool:
    """Return True when filename matches the directional-survey pattern used
    by rrc_completions_extractor.py."""
    low = filename.lower()
    return "directional" in low and "survey" in low


def _synthetic_href_for_ed(ed: "ExtractedDocument") -> str:
    """Stable, per-ED synthetic href used as the unique dedup key.

    We cannot recover the original RRC URL for already-downloaded files so we
    derive a key from the file path / neubus filename.  The key must be unique
    *within* an api_number to satisfy the UniqueConstraint(api_number, href).

    Format: ``ed:<pk>`` — guaranteed unique by primary-key.
    """
    return f"ed:{ed.pk}"


def _raw_api_variants(api_norm: str) -> list[str]:
    """Return all raw api_number strings that would normalize to *api_norm*.

    EDs stored before the normalization convention was enforced may carry
    shorter raw forms (e.g. "3001528841", "4246137851").  We include the
    14-digit canonical form plus the 10-digit and 8-digit suffixes so
    filter(api_number__in=raw_variants) catches all of them.

    The function does NOT query the DB — it just generates candidate strings.
    """
    variants: list[str] = [api_norm]
    # 10-digit: strip trailing "0000"
    if api_norm.endswith("0000") and len(api_norm) == 14:
        variants.append(api_norm[:10])
    # 8-digit (TX): strip leading "42" and trailing "0000"
    if api_norm.startswith("42") and api_norm.endswith("0000") and len(api_norm) == 14:
        variants.append(api_norm[2:10])
    return variants


def _synthetic_href_for_disk_file(api_norm: str, filename: str) -> str:
    """Stable key for an orphan disk file (no ExtractedDocument).

    Format: ``disk:<api_norm>:<filename>`` — unique per (api, filename).
    """
    return f"disk:{api_norm}:{filename}"


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------

class Command(BaseCommand):
    help = (
        "Backfill RetrievedDocument manifest rows for wells researched before "
        "the manifest feature shipped.  Idempotent — safe to re-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--well",
            type=str,
            default=None,
            help="Restrict to a single 14-digit API number.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Print what WOULD be created/updated without writing anything.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        well_filter: Optional[str] = options.get("well")

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no rows will be written.\n"))

        # ------------------------------------------------------------------
        # Determine the set of API numbers to process
        # ------------------------------------------------------------------
        api_numbers: list[str] = []

        if well_filter:
            norm = normalize_api_14digit(well_filter)
            if not norm:
                self.stderr.write(
                    self.style.ERROR(f"Cannot normalize API '{well_filter}'.  Aborting.")
                )
                return
            api_numbers = [norm]
        else:
            # Union of all api_numbers from EDs + api-dirs on disk.
            # EDs may store raw/un-normalized api_numbers (e.g. "3001528841"
            # instead of "30015288410000").  Normalize everything so each well
            # is processed exactly once.
            ed_raw_apis = ExtractedDocument.objects.values_list(
                "api_number", flat=True
            ).distinct()
            ed_apis: set[str] = set()
            for raw in ed_raw_apis:
                norm_val = normalize_api_14digit(raw)
                if norm_val:
                    ed_apis.add(norm_val)

            disk_root = _media_completions_root()
            disk_apis: set[str] = set()
            if disk_root.exists():
                for entry in disk_root.iterdir():
                    if entry.is_dir():
                        norm_val = normalize_api_14digit(entry.name)
                        if norm_val:
                            disk_apis.add(norm_val)

            all_apis = ed_apis | disk_apis
            api_numbers = sorted(all_apis)

        # ------------------------------------------------------------------
        # Counters
        # ------------------------------------------------------------------
        wells_processed = 0
        ed_created = 0
        ed_updated = 0
        ed_skipped = 0
        disk_created = 0
        disk_skipped = 0

        for api_norm in api_numbers:
            well_obj = WellRegistry.objects.filter(api14=api_norm).first()

            w_ed_created, w_ed_updated, w_ed_skipped = self._process_extracted_documents(
                api_norm=api_norm,
                well_obj=well_obj,
                dry_run=dry_run,
            )
            w_disk_created, w_disk_skipped = self._process_disk_orphans(
                api_norm=api_norm,
                well_obj=well_obj,
                dry_run=dry_run,
            )

            wells_processed += 1
            ed_created += w_ed_created
            ed_updated += w_ed_updated
            ed_skipped += w_ed_skipped
            disk_created += w_disk_created
            disk_skipped += w_disk_skipped

            if w_ed_created or w_ed_updated or w_disk_created:
                self.stdout.write(
                    f"  {api_norm}: ED +{w_ed_created}~{w_ed_updated}  "
                    f"disk +{w_disk_created}"
                )

        # ------------------------------------------------------------------
        # Summary
        # ------------------------------------------------------------------
        mode = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{mode}Backfill complete.\n"
                f"  Wells processed  : {wells_processed}\n"
                f"  ED-sourced rows  : created={ed_created}  "
                f"updated/linked={ed_updated}  skipped={ed_skipped}\n"
                f"  Disk-only rows   : created={disk_created}  "
                f"skipped(orphan-free)={disk_skipped}"
            )
        )

    # ------------------------------------------------------------------
    # Source A: ExtractedDocuments
    # ------------------------------------------------------------------

    def _process_extracted_documents(
        self,
        api_norm: str,
        well_obj: Optional["WellRegistry"],
        dry_run: bool,
    ) -> tuple[int, int, int]:
        """Create/update a RetrievedDocument for each ExtractedDocument.

        Returns (created, updated, skipped).
        """
        created = updated = skipped = 0

        # EDs may have been saved with raw (un-normalized) api_numbers such as
        # "3001528841" instead of "30015288410000".  Build the set of all raw
        # forms that normalize to api_norm so we catch them all.
        raw_variants: list[str] = _raw_api_variants(api_norm)
        eds = ExtractedDocument.objects.filter(api_number__in=raw_variants)

        for ed in eds.iterator():
            # Determine filename and local_path from the ED
            if ed.source_path:
                # source_path is an absolute or relative path to the PDF on disk
                local_path = ed.source_path
                filename = os.path.basename(local_path)
            elif ed.neubus_filename:
                # Neubus-sourced: we have the original filename but path may vary
                filename = ed.neubus_filename
                local_path = ""
            else:
                # No file reference — still create the manifest row (no local_path)
                filename = f"{ed.document_type}_{api_norm}_{ed.pk}.pdf"
                local_path = ""

            href = _synthetic_href_for_ed(ed)

            # Decide index_status from the ED's own status
            index_status = ed.status  # "success" | "partial" | "error" | "unsupported"

            defaults = {
                "well": well_obj,
                "filename": filename,
                "local_path": local_path,
                "kind": ed.document_type,
                "index_status": index_status,
                "extracted_document": ed,
                "source_type": ed.source_type if ed.source_type else "rrc",
            }

            if not dry_run:
                _, was_created = RetrievedDocument.objects.update_or_create(
                    api_number=api_norm,
                    href=href,
                    defaults=defaults,
                )
                if was_created:
                    created += 1
                else:
                    updated += 1
            else:
                # In dry-run, check existence to report accurately
                exists = RetrievedDocument.objects.filter(
                    api_number=api_norm, href=href
                ).exists()
                if exists:
                    updated += 1
                else:
                    created += 1

        return created, updated, skipped

    # ------------------------------------------------------------------
    # Source B: Orphan disk files (no matching ExtractedDocument)
    # ------------------------------------------------------------------

    def _process_disk_orphans(
        self,
        api_norm: str,
        well_obj: Optional["WellRegistry"],
        dry_run: bool,
    ) -> tuple[int, int]:
        """Walk MEDIA_ROOT/rrc/completions/<digits>/ for PDFs that have no
        ExtractedDocument whose source_path matches.

        Returns (created, skipped).
        """
        created = skipped = 0

        # api_norm is 14-digit; disk dirs are named by the raw digit strip from
        # the download (8, 10, or 14 digits).  Try the exact api_norm first,
        # then the 8-digit suffix (rrc_completions_extractor uses api[-8:] for
        # the search but saves files under the full raw API digits passed in).
        disk_root = _media_completions_root()
        candidate_dirs: list[Path] = []

        # Exact match on 14-digit dir
        exact = disk_root / api_norm
        if exact.is_dir():
            candidate_dirs.append(exact)

        # Fallback: any dir whose normalize_api_14digit matches api_norm
        if not candidate_dirs:
            for entry in disk_root.iterdir():
                if entry.is_dir():
                    if normalize_api_14digit(entry.name) == api_norm:
                        candidate_dirs.append(entry)

        # Collect all source_paths known for this api (so we can skip non-orphans).
        # Use all raw api_number variants to catch un-normalized stored values.
        raw_variants: list[str] = _raw_api_variants(api_norm)
        known_paths: set[str] = set(
            ExtractedDocument.objects.filter(api_number__in=raw_variants)
            .exclude(source_path="")
            .values_list("source_path", flat=True)
        )

        for api_dir in candidate_dirs:
            for pdf_path in api_dir.glob("*.pdf"):
                str_path = str(pdf_path)

                # Skip if this file is already covered by an ExtractedDocument
                if str_path in known_paths:
                    skipped += 1
                    continue

                filename = pdf_path.name
                href = _synthetic_href_for_disk_file(api_norm, filename)

                is_dir_survey = _is_directional(filename)
                index_status = "skipped_directional" if is_dir_survey else "pending"

                # Compute file hash cheaply (reads once)
                file_hash = _sha256_of_file(pdf_path) if not dry_run else ""

                defaults = {
                    "well": well_obj,
                    "filename": filename,
                    "local_path": str_path,
                    "file_hash": file_hash,
                    "kind": "directional" if is_dir_survey else "other",
                    "index_status": index_status,
                    "extracted_document": None,
                    "source_type": "rrc",
                }

                if not dry_run:
                    _, was_created = RetrievedDocument.objects.update_or_create(
                        api_number=api_norm,
                        href=href,
                        defaults=defaults,
                    )
                    if was_created:
                        created += 1
                    else:
                        skipped += 1
                else:
                    exists = RetrievedDocument.objects.filter(
                        api_number=api_norm, href=href
                    ).exists()
                    if not exists:
                        created += 1
                    else:
                        skipped += 1

        return created, skipped
